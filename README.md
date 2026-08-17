# rmbot — reMarkable → Claude → Telegram

Syncs reMarkable notebooks (`rm2md.sh`), transcribes new handwritten pages with
Claude (per-page cache: each page is only ever transcribed once), and runs a
Telegram bot (`bot/rmbot.py`) that

- sends a **scheduled digest** per notebook when new pages appear, following
  the notebook's goal/prompt in `config.yaml`, and
- lets you **chat about a notebook** with its full transcript + recent chat
  history as context.

## Schedules

Digests run on a schedule set in `config.yaml`: `digest_schedule` is the
global default, and each notebook can override it with its own `schedule`.
Two forms are supported:

- `daily@HH:MM` — a fixed local time of day (uses the `TZ` from `.env`)
- `every <N>h` / `every <N>d` — an interval

The bot checks schedules every 10 minutes; when a notebook is due it syncs,
transcribes, and digests only the pages it hasn't digested before. The first
run after adding a notebook silently sets a baseline — digests cover pages
added from then on. `/sync` in Telegram forces a sync + digest of all
notebooks regardless of schedule.

## Deploy (VPS, Docker)

```sh
cp bot/config.example.yaml config.yaml   # edit: chat_id, notebooks, prompts, schedules
cp .env.example .env                     # edit: tokens (see below)
docker compose build
# reMarkable auth: copy an existing token into ./rmapi/rmapi.conf
#   (macOS: ~/Library/Application Support/rmapi/rmapi.conf, Linux: ~/.config/rmapi/rmapi.conf)
# or pair fresh with a one-time code from my.remarkable.com:
#   docker compose run --rm rmbot rmapi
docker compose up -d
```

Tokens for `.env`:

- `TELEGRAM_BOT_TOKEN` — create a bot with [@BotFather](https://t.me/BotFather).
- `CLAUDE_CODE_OAUTH_TOKEN` — run `claude setup-token` on any machine where
  you're logged in to Claude (uses your subscription; token lasts ~1 year).

Optional: seed `./data/rm-sync/` with an existing rm-sync dir (its `cache/` is
machine-portable) to avoid re-transcribing already-processed pages.

## Operate

- Telegram: `/notebooks`, `/use <name>`, `/sync`, `/status`, or just chat.
- Logs: `docker compose logs -f`
- Config changes (schedules, prompts, notebooks): `docker compose restart` —
  `config.yaml` is only read at startup.
- Update: `git pull && docker compose up -d --build`
- All state is flat files in `./data` — back that up and you can rebuild
  everything else from scratch.

## Local use without the bot

`./rm2md.sh` still works standalone (see its header). Notably
`./rm2md.sh -s <doc>` syncs and transcribes a single document.
