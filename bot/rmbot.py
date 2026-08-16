#!/usr/bin/env python3
"""rmbot — reMarkable -> Claude -> Telegram bot.

One process, two loops:
  - telegram_loop: long-polls getUpdates; chat messages are answered with the
    notebook transcript + recent chat history as context (via `claude -p`).
  - digest_loop: once a day (digest_time, container TZ) runs rm2md.sh to
    sync + transcribe, then sends one digest message per notebook that got
    new pages, following that notebook's prompt/goal from config.yaml.

State lives in flat files under DATA_DIR (default /data):
  rm-sync/        rm2md.sh working dir (mirror, page cache, markdown)
  chat/<name>.jsonl   per-notebook conversation history
  bot_state.json      active notebook + telegram update offset
"""

import asyncio
import datetime as dt
import json
import os
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


def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    cfg.setdefault("model", "opus")
    cfg.setdefault("digest_time", "08:00")
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"active": None, "offset": 0}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state))


def log(*args) -> None:
    print(f"[{dt.datetime.now():%F %T}]", *args, flush=True)


# ── Claude ─────────────────────────────────────────────────────────────────
def run_claude(prompt: str, model: str) -> str:
    """Run `claude -p` with no tools; all context is in the prompt."""
    proc = subprocess.run(
        ["claude", "-p", "--model", model],
        input=prompt, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"claude -p failed: {proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


# ── Notebook context ───────────────────────────────────────────────────────
def transcript_path(cfg: dict, name: str) -> Path:
    folder = os.environ.get("RM_FOLDER", "Prep")
    return WORK_DIR / "md" / folder / f"{name}.md"


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


def build_chat_prompt(cfg: dict, name: str, user_msg: str) -> str:
    nb = cfg["notebooks"][name]
    md = transcript_path(cfg, name)
    transcript = md.read_text()[-TRANSCRIPT_MAX_CHARS:] if md.exists() else "(no transcript yet)"
    return f"""You are a Telegram assistant for the reMarkable notebook '{name}'.
Goal of this notebook: {nb.get('goal', '(none)')}
Standing instructions: {nb.get('prompt', '(none)')}

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
def cache_snapshot(cfg: dict) -> dict[str, set]:
    folder = os.environ.get("RM_FOLDER", "Prep")
    snap = {}
    for name in cfg["notebooks"]:
        d = WORK_DIR / "cache" / folder / name
        snap[name] = {p.name for p in d.glob("*.md")} if d.is_dir() else set()
    return snap


def run_sync() -> None:
    env = {**os.environ, "WORK_DIR": str(WORK_DIR)}
    proc = subprocess.run([RM2MD], env=env, capture_output=True, text=True, timeout=7200)
    if proc.returncode != 0:
        raise RuntimeError(f"rm2md.sh failed:\n{proc.stdout[-1000:]}\n{proc.stderr[-1000:]}")


def new_pages_text(cfg: dict, name: str, new_files: set) -> str:
    folder = os.environ.get("RM_FOLDER", "Prep")
    d = WORK_DIR / "cache" / folder / name
    paths = sorted((d / f for f in new_files), key=lambda p: p.stat().st_mtime)
    return "\n\n--- (new page) ---\n\n".join(p.read_text() for p in paths)


def build_digest_prompt(cfg: dict, name: str, pages: str) -> str:
    nb = cfg["notebooks"][name]
    return f"""New handwritten pages appeared in the reMarkable notebook '{name}'.
Goal of this notebook: {nb.get('goal', '(none)')}
Standing instructions for the daily digest: {nb.get('prompt', '(none)')}

New page transcripts:
<new_pages>
{pages}
</new_pages>

Recent conversation with the user (for tone/continuity):
<chat>
{recent_chat(name, 10)}
</chat>

Write the Telegram digest message to send to the user, following the standing
instructions. Be concise. Plain text only (no markdown headers or code fences)."""


async def run_digest(cfg: dict, chat_id: int, manual: bool = False) -> None:
    log("digest: syncing...")
    before = cache_snapshot(cfg)
    await asyncio.to_thread(run_sync)
    after = cache_snapshot(cfg)

    changed = {n: after[n] - before[n] for n in cfg["notebooks"] if after[n] - before[n]}
    if not changed:
        log("digest: nothing new")
        if manual:
            await send(chat_id, "Sync done — nothing new in any configured notebook.")
        return

    for name, new_files in changed.items():
        pages = new_pages_text(cfg, name, new_files)
        msg = await asyncio.to_thread(
            run_claude, build_digest_prompt(cfg, name, pages), cfg["model"]
        )
        await send(chat_id, f"📓 {name}\n\n{msg}")
        append_chat(name, "assistant", f"[daily digest] {msg}")
        log(f"digest: sent for {name} ({len(new_files)} new pages)")


async def digest_loop(cfg: dict, chat_id: int) -> None:
    while True:
        hh, mm = map(int, cfg["digest_time"].split(":"))
        now = dt.datetime.now()
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        log(f"digest: next run at {nxt}")
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await run_digest(cfg, chat_id)
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            log("digest error:", e)
            await send(chat_id, f"⚠️ digest failed: {e}")


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
/sync — sync + digest now (instead of waiting for the daily run)
/status — active notebook and next digest time
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
        await run_digest(cfg, chat_id, manual=True)
    elif text.startswith("/status"):
        await send(chat_id, f"Active notebook: {state['active'] or '(none — /use <name>)'}\n"
                            f"Daily digest at {cfg['digest_time']} ({os.environ.get('TZ', 'UTC')})")
    else:
        name = state["active"]
        if not name:
            await send(chat_id, "No active notebook. " + HELP)
            return
        append_chat(name, "user", text)
        reply = await asyncio.to_thread(
            run_claude, build_chat_prompt(cfg, name, text), cfg["model"]
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
    log(f"rmbot up — notebooks: {', '.join(cfg['notebooks'])}, "
        f"digest at {cfg['digest_time']}")
    await asyncio.gather(
        telegram_loop(cfg, state, chat_id),
        digest_loop(cfg, chat_id),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
