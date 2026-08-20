#!/usr/bin/env python3
"""rmbot — reMarkable -> Claude -> Telegram bot.

One process, two loops:
  - telegram_loop: long-polls getUpdates; chat messages are answered with the
    notebook transcript + recent chat history as context (via `claude -p`).
  - digest_loop: ticks every 10 minutes; when a notebook's schedule is due it
    runs rm2md.sh (sync + transcribe) and sends a digest of the pages not yet
    digested for that notebook, following its prompt/goal from config.yaml.

Schedules (per notebook `schedule:`, falling back to global `digest_schedule`):
  "daily@HH:MM"   fixed local time of day (TZ env)
  "every Nh"      interval (also "every Nd")

State lives in flat files under DATA_DIR (default /data):
  rm-sync/            rm2md.sh working dir (mirror, page cache, markdown)
  chat/<name>.jsonl   per-notebook conversation history
  bot_state.json      active notebook, telegram offset, per-notebook
                      last_digest + seen_pages (which cache pages were digested)
"""

import asyncio
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import yaml

# ── Config / paths ─────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("RMBOT_DATA", "/data"))
CONFIG_PATH = Path(os.environ.get("RMBOT_CONFIG", "/app/config.yaml"))
RM2MD = os.environ.get("RM2MD", "/app/rm2md.sh")
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

WORK_DIR = DATA_DIR / "rm-sync"
CHAT_DIR = DATA_DIR / "chat"
STATE_PATH = DATA_DIR / "bot_state.json"

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CHAT_TURNS_IN_CONTEXT = 20
TRANSCRIPT_MAX_CHARS = 300_000  # tail of the transcript fed to Claude
TICK_SECONDS = 600              # how often the digest loop checks schedules


# ── Schedules ──────────────────────────────────────────────────────────────
def parse_schedule(s: str) -> tuple:
    """'every 6h'/'every 2d' -> ('every', seconds); 'daily@08:00' -> ('daily', (h, m))."""
    s = s.strip().lower()
    m = re.fullmatch(r"every\s+(\d+)\s*([hd])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return ("every", n * (3600 if unit == "h" else 86400))
    m = re.fullmatch(r"daily@(\d{1,2}):(\d{2})", s)
    if m:
        h, mm = int(m.group(1)), int(m.group(2))
        if h < 24 and mm < 60:
            return ("daily", (h, mm))
    raise ValueError(f"bad schedule '{s}' — use 'every <N>h', 'every <N>d' or 'daily@HH:MM'")


def schedule_of(cfg: dict, name: str) -> tuple:
    return parse_schedule(cfg["notebooks"][name].get("schedule") or cfg["digest_schedule"])


def web_search_of(cfg: dict, name: str) -> bool:
    return bool(cfg["notebooks"][name].get("web_search", cfg.get("web_search", False)))


def is_due(sched: tuple, last: dt.datetime | None, now: dt.datetime) -> bool:
    kind, val = sched
    if last is None:
        return True  # first sighting: tick runs, sets the baseline silently
    if kind == "every":
        return (now - last).total_seconds() >= val
    occ = now.replace(hour=val[0], minute=val[1], second=0, microsecond=0)
    if occ > now:
        occ -= dt.timedelta(days=1)  # most recent scheduled occurrence
    return last < occ


def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    # digest_time is the pre-schedule config key; honour it as the default.
    cfg.setdefault("digest_schedule", f"daily@{cfg['digest_time']}" if cfg.get("digest_time")
                   else "daily@08:00")
    cfg.setdefault("model", "opus")
    for name in cfg["notebooks"]:  # fail fast on typos
        schedule_of(cfg, name)
    return cfg


def load_state() -> dict:
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    state.setdefault("active", None)
    state.setdefault("offset", 0)
    state.setdefault("last_digest", {})
    state.setdefault("seen_pages", {})
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state))


def last_digest(state: dict, name: str) -> dt.datetime | None:
    iso = state["last_digest"].get(name)
    return dt.datetime.fromisoformat(iso) if iso else None


def log(*args) -> None:
    print(f"[{dt.datetime.now():%F %T}]", *args, flush=True)


# ── Claude ─────────────────────────────────────────────────────────────────
def run_claude(prompt: str, model: str, web_search: bool = False) -> str:
    """Run `claude -p`; all context is in the prompt. Optionally allow WebSearch."""
    cmd = ["claude", "-p", "--model", model]
    if web_search:
        cmd += ["--allowedTools", "WebSearch"]
    proc = subprocess.run(
        cmd,
        input=prompt, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"claude -p failed: {proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


# ── Notebook context ───────────────────────────────────────────────────────
def transcript_path(name: str) -> Path:
    folder = os.environ.get("RM_FOLDER", "Prep")
    return WORK_DIR / "md" / folder / f"{name}.md"


def cache_files(name: str) -> set:
    folder = os.environ.get("RM_FOLDER", "Prep")
    d = WORK_DIR / "cache" / folder / name
    return {p.name for p in d.glob("*.md")} if d.is_dir() else set()


def chat_log_path(name: str) -> Path:
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    return CHAT_DIR / f"{name}.jsonl"


def append_chat(name: str, role: str, text: str) -> None:
    entry = {"t": dt.datetime.now().isoformat(timespec="seconds"), "role": role, "text": text}
    with chat_log_path(name).open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def recent_chat(name: str, turns: int = CHAT_TURNS_IN_CONTEXT) -> str:
    path = chat_log_path(name)
    if not path.exists():
        return "(no previous conversation)"
    lines = path.read_text().strip().splitlines()[-turns:]
    out = []
    for line in lines:
        e = json.loads(line)
        out.append(f"{e['role'].capitalize()}: {e['text']}")
    return "\n".join(out) or "(no previous conversation)"


def web_search_note(cfg: dict, name: str) -> str:
    if not web_search_of(cfg, name):
        return ""
    return ("\nYou have web search available — use it to fact-check concrete "
            "claims, numbers, or assumptions in the notes when it materially helps.\n")


def build_chat_prompt(cfg: dict, name: str, user_msg: str) -> str:
    nb = cfg["notebooks"][name]
    md = transcript_path(name)
    transcript = md.read_text()[-TRANSCRIPT_MAX_CHARS:] if md.exists() else "(no transcript yet)"
    return f"""You are a Telegram assistant for the reMarkable notebook '{name}'.
Goal of this notebook: {nb.get('goal', '(none)')}
Standing instructions: {nb.get('prompt', '(none)')}
{web_search_note(cfg, name)}

Full transcript of the notebook (markdown, pages separated by '---'):
<transcript>
{transcript}
</transcript>

Recent conversation with the user:
<chat>
{recent_chat(name)}
</chat>

The user just wrote: {user_msg}

Reply as the assistant. Be concise and direct — this is a Telegram chat.
Plain text only (no markdown headers or code fences)."""


# ── Digest ─────────────────────────────────────────────────────────────────
def run_sync() -> None:
    env = {**os.environ, "WORK_DIR": str(WORK_DIR)}
    proc = subprocess.run([RM2MD], env=env, capture_output=True, text=True, timeout=7200)
    if proc.returncode != 0:
        raise RuntimeError(f"rm2md.sh failed:\n{proc.stdout[-1000:]}\n{proc.stderr[-1000:]}")


def new_pages_text(name: str, new_files: set) -> str:
    folder = os.environ.get("RM_FOLDER", "Prep")
    d = WORK_DIR / "cache" / folder / name
    paths = sorted((d / f for f in new_files), key=lambda p: p.stat().st_mtime)
    return "\n\n--- (new page) ---\n\n".join(p.read_text() for p in paths)


def build_digest_prompt(cfg: dict, name: str, pages: str) -> str:
    nb = cfg["notebooks"][name]
    return f"""New handwritten pages appeared in the reMarkable notebook '{name}'.
Goal of this notebook: {nb.get('goal', '(none)')}
Standing instructions for the digest: {nb.get('prompt', '(none)')}
{web_search_note(cfg, name)}

New page transcripts:
<new_pages>
{pages}
</new_pages>

Recent conversation with the user (for tone/continuity):
<chat>
{recent_chat(name, 10)}
</chat>

Write the Telegram digest message to send to the user, following the standing
instructions. Be concise. Plain text only (no markdown headers or code fences).

You do not have to message the user every time: if, given the goal and standing
instructions, nothing here genuinely warrants their attention right now, reply
with exactly SKIP (nothing else) — these pages will be carried over and
reconsidered together with newer ones at the next scheduled check."""


async def digest_tick(cfg: dict, state: dict, chat_id: int,
                      names: list, manual: bool = False) -> None:
    """Sync once, then digest each notebook in `names` (pages not seen before)."""
    if not names:
        return
    log(f"digest: syncing (due: {', '.join(names)})")
    await asyncio.to_thread(run_sync)
    now = dt.datetime.now()
    for name in names:
        current = cache_files(name)
        first = name not in state["seen_pages"]
        new = current - set(state["seen_pages"].get(name, []))
        if first:
            log(f"digest: {name} baseline set ({len(current)} existing pages)")
            if manual:
                await send(chat_id, f"📓 {name}: baseline set ({len(current)} existing "
                                    "pages) — digests will cover pages added from now on.")
        elif new:
            pages = new_pages_text(name, new)
            msg = await asyncio.to_thread(
                run_claude, build_digest_prompt(cfg, name, pages), cfg["model"],
                web_search_of(cfg, name),
            )
            if msg.strip() == "SKIP":
                # Nothing worth sending: hold the pages (stay un-seen) so they
                # roll into the next scheduled check together with newer ones.
                log(f"digest: {name} held ({len(new)} new pages, nothing to say)")
                if manual:
                    await send(chat_id, f"📓 {name}: {len(new)} new pages, "
                                        "nothing worth a nudge yet.")
                state["last_digest"][name] = now.isoformat(timespec="seconds")
                continue
            await send(chat_id, f"📓 {name}\n\n{msg}")
            append_chat(name, "assistant", f"[digest] {msg}")
            log(f"digest: sent for {name} ({len(new)} new pages)")
        elif manual:
            await send(chat_id, f"📓 {name}: nothing new.")
        state["seen_pages"][name] = sorted(current)
        state["last_digest"][name] = now.isoformat(timespec="seconds")
    save_state(state)


async def digest_loop(cfg: dict, state: dict, chat_id: int) -> None:
    while True:
        try:
            now = dt.datetime.now()
            due = [n for n in cfg["notebooks"]
                   if is_due(schedule_of(cfg, n), last_digest(state, n), now)]
            await digest_tick(cfg, state, chat_id, due)
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            log("digest error:", e)
            await send(chat_id, f"⚠️ digest failed: {e}")
        await asyncio.sleep(TICK_SECONDS)


# ── Telegram ───────────────────────────────────────────────────────────────
async def send(chat_id: int, text: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(text), 4000):
            await client.post(f"{API}/sendMessage",
                              json={"chat_id": chat_id, "text": text[i:i + 4000]})


def resolve_notebook(cfg: dict, query: str) -> str | None:
    names = list(cfg["notebooks"])
    exact = [n for n in names if n.lower() == query.lower()]
    if exact:
        return exact[0]
    sub = [n for n in names if query.lower() in n.lower()]
    return sub[0] if len(sub) == 1 else None


HELP = """Commands:
/notebooks — list configured notebooks
/use <name> — switch the active notebook for chat
/sync — sync + digest all notebooks now (regardless of schedule)
/status — active notebook, schedules, last digests
Anything else — chat about the active notebook (with its transcript as context)."""


async def handle_message(cfg: dict, state: dict, chat_id: int, text: str) -> None:
    if text.startswith("/start") or text.startswith("/help"):
        await send(chat_id, HELP)
    elif text.startswith("/notebooks"):
        lines = [f"{'▶ ' if n == state['active'] else '  '}{n} — {v.get('goal', '')}"
                 for n, v in cfg["notebooks"].items()]
        await send(chat_id, "\n".join(lines))
    elif text.startswith("/use"):
        name = resolve_notebook(cfg, text[4:].strip())
        if name:
            state["active"] = name
            save_state(state)
            await send(chat_id, f"Active notebook: {name}")
        else:
            await send(chat_id, f"No unique match. Configured: {', '.join(cfg['notebooks'])}")
    elif text.startswith("/sync"):
        await send(chat_id, "Syncing…")
        await digest_tick(cfg, state, chat_id, list(cfg["notebooks"]), manual=True)
    elif text.startswith("/status"):
        lines = [f"Active notebook: {state['active'] or '(none — /use <name>)'}",
                 f"Timezone: {os.environ.get('TZ', 'UTC')}"]
        for n in cfg["notebooks"]:
            sched = cfg["notebooks"][n].get("schedule") or cfg["digest_schedule"]
            last = state["last_digest"].get(n, "never")
            lines.append(f"{n}: {sched} (last digest: {last})")
        await send(chat_id, "\n".join(lines))
    else:
        name = state["active"]
        if not name:
            await send(chat_id, "No active notebook. " + HELP)
            return
        append_chat(name, "user", text)
        reply = await asyncio.to_thread(
            run_claude, build_chat_prompt(cfg, name, text), cfg["model"],
            web_search_of(cfg, name),
        )
        append_chat(name, "assistant", reply)
        await send(chat_id, reply)


async def telegram_loop(cfg: dict, state: dict, allowed_chat_id: int) -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            try:
                r = await client.get(f"{API}/getUpdates",
                                     params={"offset": state["offset"] + 1, "timeout": 50})
                for upd in r.json().get("result", []):
                    state["offset"] = upd["update_id"]
                    save_state(state)
                    msg = upd.get("message") or {}
                    text = msg.get("text")
                    chat_id = msg.get("chat", {}).get("id")
                    if not text or chat_id != allowed_chat_id:
                        continue
                    try:
                        await handle_message(cfg, state, chat_id, text)
                    except Exception as e:  # noqa: BLE001
                        log("handler error:", e)
                        await send(chat_id, f"⚠️ error: {e}")
            except httpx.HTTPError as e:
                log("telegram poll error:", e)
                await asyncio.sleep(5)


# ── Main ───────────────────────────────────────────────────────────────────
async def main() -> None:
    cfg = load_config()
    state = load_state()
    chat_id = int(cfg["telegram"]["chat_id"])
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    scheds = ", ".join(f"{n}={cfg['notebooks'][n].get('schedule') or cfg['digest_schedule']}"
                       for n in cfg["notebooks"])
    log(f"rmbot up — {scheds}")
    await asyncio.gather(
        telegram_loop(cfg, state, chat_id),
        digest_loop(cfg, state, chat_id),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
