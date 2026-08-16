#!/usr/bin/env bash
# rm2md.sh — mirror a reMarkable cloud folder via rmapi and transcribe
# handwritten notebooks to Markdown using Claude Code (subscription, no API key).
#
# Pipeline: rmapi mget -i  ->  unzip .rmdoc  ->  rmc (.rm v6 -> SVG)  ->
#           cairosvg (SVG -> PNG)  ->  claude -p (vision transcription)  ->  .md
#
# Usage:
#   rm2md.sh                     Sync folder ($RM_FOLDER, default "Prep") and
#                                transcribe everything new/changed.
#   rm2md.sh <doc>               Transcribe ONE document and exit (no rmapi sync;
#                                re-assembles the .md, but pages whose content is
#                                unchanged reuse the page cache). <doc> can be:
#                                  - a path to a .rmdoc/.zip file
#                                  - a document name (fuzzy-matched in the mirror)
#   rm2md.sh -s <doc>            Same, but rmapi-sync the folder first so you
#                                get the latest version of that doc.
#   rm2md.sh -l                  List documents in the local mirror and exit.
#   rm2md.sh -d                  Check dependencies and exit.
#   rm2md.sh -h                  Help.
#
# Options / env vars:
#   -m <model>   Claude model (default: opus). Env: CLAUDE_MODEL
#   -s           Also run rmapi sync in single-doc mode
#   RM_FOLDER    reMarkable cloud folder to mirror   (default: Prep)
#   WORK_DIR     working directory                   (default: ~/rm-sync)
#
# Dependencies:
#   macOS:  brew install poppler jq cairo libffi        # pdftoppm, jq, cairo
#   Ubuntu: sudo apt install poppler-utils jq unzip libcairo2
#   both:   pip install --upgrade rmc rmscene cairosvg
#           rmapi (ddvk fork) authenticated once; claude (Claude Code CLI) logged in once
#
# Cron:   0 * * * * $HOME/bin/rm2md.sh >> $HOME/rm-sync/cron.log 2>&1

set -euo pipefail

# Let cairosvg's Python find Homebrew's libcairo (not on the default dyld path).
if [ -d /opt/homebrew/lib ]; then
  export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
fi

### ── Config ──────────────────────────────────────────────────────────────
RM_FOLDER="${RM_FOLDER:-Prep}"            # reMarkable cloud folder to mirror
WORK_DIR="${WORK_DIR:-$HOME/rm-sync}"     # everything lives under here
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-opus}"      # default model: Opus
PNG_WIDTH="${PNG_WIDTH:-1404}"            # native reMarkable width, plenty for OCR
### ────────────────────────────────────────────────────────────────────────

RAW_DIR="$WORK_DIR/raw"                   # rmapi mirror (.rmdoc files)
MD_DIR="$WORK_DIR/md/$RM_FOLDER"          # markdown output
CACHE_DIR="$WORK_DIR/cache/$RM_FOLDER"    # per-page transcripts, keyed by source-page sha256
STATE_FILE="$WORK_DIR/state.json"         # doc-name -> sha256, to skip unchanged docs
LOG_FILE="$WORK_DIR/rm2md.log"

mkdir -p "$RAW_DIR" "$MD_DIR" "$CACHE_DIR"
[ -f "$STATE_FILE" ] || echo '{}' > "$STATE_FILE"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"; }

usage() { sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# Portable sha256 (macOS has shasum, Linux has sha256sum)
hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ── Dependency check ───────────────────────────────────────────────────────
deps_check() {
  local missing=0
  local dep hint
  for dep in rmapi jq unzip pdftoppm rmc cairosvg "$CLAUDE_BIN"; do
    if ! command -v "$dep" >/dev/null 2>&1; then
      case "$dep" in
        pdftoppm) hint="brew install poppler   (or apt install poppler-utils)" ;;
        jq)       hint="brew install jq" ;;
        rmc)      hint="pip install --upgrade rmc rmscene" ;;
        cairosvg) hint="pip install cairosvg  +  brew install cairo libffi (or apt install libcairo2)" ;;
        rmapi)    hint="install ddvk/rmapi and run 'rmapi' once to authenticate" ;;
        *)        hint="install '$dep' and make sure it is on PATH" ;;
      esac
      echo "MISSING: $dep    -> $hint" >&2
      missing=1
    fi
  done
  if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
    echo "MISSING: sha256sum/shasum" >&2; missing=1
  fi
  if [ "$missing" -eq 1 ]; then
    echo "Install the missing dependencies above, then re-run." >&2
    return 1
  fi
  echo "All dependencies found:"
  echo "  rmc:      $(command -v rmc)  ($(rmc --version 2>/dev/null || echo 'version unknown'))"
  echo "  pdftoppm: $(command -v pdftoppm)"
  echo "  cairosvg: $(command -v cairosvg)"
  echo "  claude:   $(command -v "$CLAUDE_BIN")"
  echo "  rmapi:    $(command -v rmapi)"
}

# Prompt for transcribing a single page image ($1 = PNG filename).
# One claude call per page keeps the per-page cache trustworthy: a page's
# transcript can never bleed into (or out of) a neighbouring page.
page_prompt() {
  cat <<EOF
You are transcribing one page of handwritten notes from a reMarkable tablet.
Read the image file $1 in the current directory (ignore any other files) and
transcribe the handwriting into clean Markdown:
- Preserve structure: headings, bullet lists, numbered lists, checkboxes (- [ ] / - [x]), tables.
- For sketches/diagrams, add a brief one-line description in italics instead of skipping them.
- Keep the original language of the notes (may be Spanish, English, or mixed). Do not translate.
- Mark words you cannot read as [?]. Do not guess silently.
Output ONLY the Markdown transcript of this page. No preamble, no commentary, no outer code fence.
EOF
}

# ── Sync the folder from the reMarkable cloud ─────────────────────────────
sync_folder() {
  log "Syncing '$RM_FOLDER' from reMarkable cloud..."
  ( cd "$RAW_DIR" && rmapi mget -i "$RM_FOLDER" ) >>"$LOG_FILE" 2>&1 \
    || { log "ERROR: rmapi mget failed (token expired? protocol change?)"; exit 1; }
}

# ── List documents currently in the local mirror ──────────────────────────
list_docs() {
  find "$RAW_DIR" -type f \( -name '*.rmdoc' -o -name '*.zip' \) \
    | sed -e "s|^$RAW_DIR/||" | sort
}

# ── Resolve a user-supplied <doc> argument to a file in the mirror ────────
resolve_doc() {
  local target="$1"
  # Direct path?
  if [ -f "$target" ]; then printf '%s\n' "$target"; return 0; fi
  # Exact name (with or without extension) inside the mirror
  local matches
  matches=$(find "$RAW_DIR" -type f \( -name "$target" -o -name "$target.rmdoc" -o -name "$target.zip" \))
  # Fall back to case-insensitive substring match
  if [ -z "$matches" ]; then
    matches=$(find "$RAW_DIR" -type f \( -name '*.rmdoc' -o -name '*.zip' \) -iname "*$target*")
  fi
  local count
  count=$(printf '%s' "$matches" | grep -c . || true)
  if [ "$count" -eq 0 ]; then
    echo "No document matching '$target' in $RAW_DIR." >&2
    echo "Available documents:" >&2
    list_docs >&2
    return 1
  elif [ "$count" -gt 1 ]; then
    echo "Ambiguous match for '$target':" >&2
    printf '%s\n' "$matches" | sed -e "s|^$RAW_DIR/|  |" >&2
    echo "Be more specific." >&2
    return 1
  fi
  printf '%s\n' "$matches"
}

# ── Process one document: render pages + transcribe ───────────────────────
# $1 = path to .rmdoc/.zip   $2 = "force" to ignore the state hash
process_doc() {
  local doc="$1" force="${2:-}"
  local base name hash prev tmp
  base=$(basename "$doc")
  name="${base%.*}"
  hash=$(hash_file "$doc")
  prev=$(jq -r --arg k "$name" '.[$k] // empty' "$STATE_FILE")

  if [ "$hash" = "$prev" ] && [ "$force" != "force" ]; then
    return 2   # unchanged
  fi

  log "Processing: $name"
  tmp=$(mktemp -d)

  unzip -qo "$doc" -d "$tmp"

  local content_file uuid
  content_file=$(find "$tmp" -maxdepth 1 -name '*.content' | head -n1)
  if [ -z "$content_file" ]; then
    log "  WARN: no .content file inside $base — skipping"
    rm -rf "$tmp"; return 1
  fi
  uuid=$(basename "$content_file" .content)

  local pages_dir="$tmp/pages"
  mkdir -p "$pages_dir"
  local err=""

  # Per-page transcript cache, content-addressed by the SOURCE page:
  #   notebook page -> sha256 of its .rm file
  #   PDF page      -> sha256 of the PDF, plus the page number
  # Source-keyed entries are portable across machines and rendering toolchains
  # (rmc/cairo versions and PNG_WIDTH don't affect the key), and already-cached
  # notebook pages skip rendering entirely. Changing CLAUDE_MODEL does not
  # invalidate the cache — clear $CACHE_DIR to re-transcribe with a new model.
  local doc_cache="$CACHE_DIR/$name"
  mkdir -p "$doc_cache"

  local page_keys=() page_pngs=()   # parallel arrays, in document page order

  if [ -f "$tmp/$uuid.pdf" ]; then
    # PDF-backed document (annotated PDF): render the PDF base layer.
    # NOTE: handwritten annotations are NOT overlaid — pure notebooks are
    # fully handled, annotated PDFs only get the base PDF.
    log "  PDF-backed document: rendering base PDF (annotations not overlaid)"
    if ! err=$(pdftoppm -png -r 150 "$tmp/$uuid.pdf" "$pages_dir/page" 2>&1); then
      log "  ERROR: pdftoppm failed: ${err:-unknown error}"
      rm -rf "$tmp"; return 1
    fi
    # pdftoppm pads page numbers to a fixed width, so plain sort is safe.
    local f n=0
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      mv "$f" "$pages_dir/$(printf 'page-%03d.png' "$n")"
      n=$((n + 1))
    done < <(find "$pages_dir" -name 'page-*.png' ! -name 'page-[0-9][0-9][0-9].png' | sort)
    local pdfhash
    pdfhash=$(hash_file "$tmp/$uuid.pdf")
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      page_keys+=("$pdfhash-$(basename "$f" .png)")
      page_pngs+=("$f")
    done < <(find "$pages_dir" -name 'page-[0-9][0-9][0-9].png' | sort)
  else
    # Pure handwritten notebook: page order comes from the .content JSON.
    # Firmware 3.x uses .cPages.pages[]; older exports used .pages[].
    local page_ids
    page_ids=$(jq -r '
      if .cPages then
        .cPages.pages[] | select(has("deleted") | not) | .id
      else
        .pages[]
      end' "$content_file")

    local pid rmfile svg png key n=0
    while IFS= read -r pid; do
      [ -n "$pid" ] || continue
      rmfile="$tmp/$uuid/$pid.rm"
      [ -f "$rmfile" ] || continue          # blank/template-only page
      key=$(hash_file "$rmfile")
      if [ -s "$doc_cache/$key.md" ]; then
        page_keys+=("$key"); page_pngs+=("")   # cached — no need to render
        continue
      fi
      svg="$tmp/p.svg"
      png="$pages_dir/$(printf 'page-%03d.png' "$n")"
      if ! err=$(rmc -t svg "$rmfile" -o "$svg" 2>&1); then
        log "  WARN: rmc failed on page $pid: $(echo "${err:-unknown error}" | tail -n 2 | tr '\n' ' ')"
        continue
      fi
      if ! err=$(cairosvg "$svg" -o "$png" \
             --output-width "$PNG_WIDTH" --background white 2>&1); then
        log "  WARN: cairosvg failed on page $pid: $(echo "${err:-unknown error}" | tail -n 2 | tr '\n' ' ')"
        continue
      fi
      page_keys+=("$key"); page_pngs+=("$png")
      n=$((n + 1))
    done <<< "$page_ids"
  fi

  local page_count=${#page_keys[@]}
  if [ "$page_count" -eq 0 ]; then
    log "  WARN: no renderable pages in $name — skipping (see errors above / $LOG_FILE)"
    rm -rf "$tmp"; return 1
  fi

  # ── Transcribe uncached pages with Claude Code, one page per call ────────
  local i key png cached new_pages=0 cached_pages=0 failed_pages=0
  for i in "${!page_keys[@]}"; do
    key="${page_keys[$i]}"
    png="${page_pngs[$i]}"
    cached="$doc_cache/$key.md"
    if [ -s "$cached" ]; then
      cached_pages=$((cached_pages + 1))
      continue
    fi
    if [ -z "$png" ]; then   # cache entry vanished after the render pass
      log "  ERROR: cached transcript for page $((i + 1)) disappeared mid-run — will retry next run"
      failed_pages=$((failed_pages + 1))
      continue
    fi
    new_pages=$((new_pages + 1))
    log "  Transcribing $(basename "$png") with Claude Code ($CLAUDE_MODEL)..."
    if ( cd "$pages_dir" && "$CLAUDE_BIN" -p "$(page_prompt "$(basename "$png")")" \
           --allowedTools "Read" --model "$CLAUDE_MODEL" ) > "$cached.tmp" 2>>"$LOG_FILE" \
       && [ -s "$cached.tmp" ]; then
      mv "$cached.tmp" "$cached"
    else
      rm -f "$cached.tmp"
      log "  ERROR: transcription failed for $(basename "$png") — will retry next run"
      failed_pages=$((failed_pages + 1))
    fi
  done

  if [ "$failed_pages" -gt 0 ]; then
    log "  ERROR: $failed_pages/$page_count pages failed for $name — not writing .md" \
        "(successful pages are cached; next run only retries the failures)"
    rm -rf "$tmp"
    return 1
  fi

  # ── Assemble the document from cached page transcripts ──────────────────
  local out_md="$MD_DIR/$name.md"
  {
    echo "---"
    echo "source: reMarkable:/$RM_FOLDER/$name"
    echo "pages: $page_count"
    echo "model: $CLAUDE_MODEL"
    echo "transcribed: $(now_iso)"
    echo "---"
    local n=1
    for i in "${!page_keys[@]}"; do
      if [ "$n" -gt 1 ]; then
        echo
        echo "---"
        echo "<!-- page $n -->"
      fi
      echo
      cat "$doc_cache/${page_keys[$i]}.md"
      n=$((n + 1))
    done
  } > "$out_md.tmp" && mv "$out_md.tmp" "$out_md"

  # Drop cache entries for pages that no longer exist in the document.
  local keep
  keep=$(printf '%s\n' "${page_keys[@]}")
  for f in "$doc_cache"/*.md; do
    [ -e "$f" ] || continue
    grep -qx "$(basename "$f" .md)" <<< "$keep" || rm -f "$f"
  done

  # Persist the doc hash only after a fully successful assembly,
  # so failures are retried on the next cron run.
  jq --arg k "$name" --arg v "$hash" '.[$k] = $v' "$STATE_FILE" \
    > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
  log "  OK -> $out_md ($page_count pages: $cached_pages cached, $new_pages new)"
  rm -rf "$tmp"
  return 0
}

# ── CLI ────────────────────────────────────────────────────────────────────
SYNC_FIRST=""
while getopts ":m:sldh" opt; do
  case "$opt" in
    m) CLAUDE_MODEL="$OPTARG" ;;
    s) SYNC_FIRST=1 ;;
    l) list_docs; exit 0 ;;
    d) deps_check; exit $? ;;
    h) usage 0 ;;
    \?) echo "Unknown option: -$OPTARG" >&2; usage 1 ;;
    :)  echo "Option -$OPTARG requires an argument." >&2; usage 1 ;;
  esac
done
shift $((OPTIND - 1))

if [ $# -gt 1 ]; then
  echo "At most one <doc> argument, please." >&2; usage 1
fi

# Fail fast (and helpfully) if tooling is missing
deps_check >/dev/null

if [ $# -eq 1 ]; then
  # ── Single-document mode: no sync (unless -s), always re-transcribe ─────
  [ -n "$SYNC_FIRST" ] && sync_folder
  doc=$(resolve_doc "$1")
  process_doc "$doc" force
  exit $?
fi

# ── Folder mode (cron): sync everything, transcribe what changed ──────────
sync_folder
processed=0; skipped=0; failed=0
while IFS= read -r -d '' doc; do
  set +e
  process_doc "$doc"
  rc=$?
  set -e
  case $rc in
    0) processed=$((processed + 1)) ;;
    2) skipped=$((skipped + 1)) ;;
    *) failed=$((failed + 1)) ;;
  esac
done < <(find "$RAW_DIR" -type f \( -name '*.rmdoc' -o -name '*.zip' \) -print0)

log "Done. Transcribed: $processed, unchanged: $skipped, failed: $failed. Output: $MD_DIR"
