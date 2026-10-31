# ByMeAudio Bot

Telegram bot for converting voice messages, video notes, audio, and video files into text.
Speech recognition runs **locally** through Faster-Whisper (CPU, int8). User recordings
are **not uploaded to an external transcription service**. Telegram still handles
transporting files to the bot.

## Features

- Supports Telegram voice messages, video notes, audio/video, and supported document uploads.
- Local Russian transcription by default; optional automatic language detection.
- Bounded in-memory queue, one request per user, and one active CPU transcription.
- Validation of file size and decoded audio duration; no unlimited RAM-holding queue.
- Lossless splitting into Telegram-sized HTML-safe messages.
- Long transcripts (over 9,500 characters) delivered as text-file attachments.
- SQLite statistics, non-destructive schema migrations and optional deletion of personal statistics.
- Private-chat-only processing by default; administrator broadcasts.

## Quick start (Docker Compose)

Requirements: Docker Engine and Docker Compose plugin.

```bash
git clone https://github.com/Brof14/ByMeAudio_bot.git
cd ByMeAudio_bot
cp .env.example .env
# Edit .env and set BOT_TOKEN (required) and ADMIN_IDS (optional).
chmod 600 .env
docker compose up -d --build
docker compose logs -f bot
```

The first start downloads a Whisper model, which can take a while. The named Docker
volumes `bymeaudio-data` and `bymeaudio-model-cache` persist the SQLite file and model
cache. **Do not run `docker compose down -v` unless you intend to delete volumes.**

To upgrade after pulling changes:

```bash
git pull --ff-only
docker compose up -d --build
```

### Migrating an existing SQLite database

If the bot previously ran without Docker, **stop it**, back up `audio_bot.db`
together with any SQLite `-wal` or `-shm` files if present, and either:

1. Keep using local Python with `DB_PATH` pointing to the existing database; or
2. Copy the database into the container's persistent `/app/data/audio_bot.db` volume
   **while the service is stopped**.

Do not start a second polling process with the same Telegram token. Initialisation
preserves legacy `transactions` and existing user statistics.

## Local Python

Requires Python 3.11+, and enough memory for the chosen Whisper model.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Configure BOT_TOKEN.
python bot.py
```

On Windows PowerShell use `.venv\\Scripts\\Activate.ps1` instead of `source`.

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `BOT_TOKEN` | Required | Token from BotFather |
| `ADMIN_IDS` | Empty | Comma-separated Telegram user IDs |
| `WHISPER_MODEL` | `base` | Tiny is faster, small can be more accurate |
| `WHISPER_BEAM_SIZE` | `2` | 1 to 10 |
| `WHISPER_CPU_THREADS` | `0` | 0 = automatic |
| `TRANSCRIPTION_LANGUAGE` | `ru` | Use `auto` for language identification |
| `MAX_FILE_SIZE_MB` | `20` | Max 20 MiB for standard Telegram Bot API |
| `MAX_DURATION_SECONDS` | `900` | Max decoded recording length, 30 to 3600 seconds |
| `MAX_PENDING_JOBS` | `4` | Includes processing and queued requests |
| `USER_COOLDOWN_SECONDS` | `3` | Delay before accepting another request |
| `DB_PATH` | `data/audio_bot.db` | Docker overrides to persistent volume |
| `LOG_PATH` | `bot.log` | Docker overrides to persistent volume |

`GROQ_API_KEY` is intentionally not supported: no Groq transcription path
exists in the application. Avoid setting it.

## Commands

`/start`: instructions, menu; `/help`: usage; `/stats`: usage totals;
`/privacy`: privacy details; `/delete_me`: delete personal statistics
after confirmation; `/broadcast <text>`: private administrator announcement.

## Operations

- The default in-memory queue is intentionally **not persistent**. Running tasks
  interrupted by a process restart are not automatically resumed, although pending
  Telegram updates are no longer deliberately dropped.
- Only run one polling instance per token. SQLite is suitable for a small bot.
- `python -m pip install -r requirements-dev.txt && pytest -q && ruff check .`
  runs the CI checks. Tests stub recognition; they do **not** download a model.
- Backup the SQLite database using the SQLite backup API, not a plain copy of a
  live WAL-enabled database. Backup the named volume to external storage.
- If the bot serves a large audience, consider a durable job queue and a
  separately managed worker before increasing concurrency.
- No benchmarked transcription latency or accuracy is guaranteed. CPU and RAM
  requirements depend on model selection and recording duration.

## Privacy and limits

Only Telegram ID, username, first name, timestamps, and usage counters are
stored in the database. Audio and transcript text are processed transiently and
not saved in that database. Some user IDs/errors can appear in rotated
operational logs. Users can remove their profile with `/delete_me`. Be mindful
of local privacy laws and obtain consent for other people's recordings.
