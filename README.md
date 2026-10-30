# ByMeAudio Bot

A Telegram bot that transcribes voice messages, video notes, audio files, and video files with **Faster-Whisper** running locally on the bot host.

## Features

- Accepts Telegram voice messages, video notes, common audio formats, and video files with an audio track.
- Runs transcription locally on CPU using Faster-Whisper and CTranslate2; audio is not sent to an external transcription API.
- Splits long transcripts into Telegram-sized messages.
- Handles transcription outside the update loop so the bot can continue responding to commands.
- Provides per-user usage statistics with `/stats`; administrator commands include broadcasts.

The default model and performance depend on your server's CPU and memory. This is a self-hosted project, not a hosted service or an uptime guarantee.

## Requirements

- Python 3.10+
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Docker and Docker Compose for the container deployment

## Run with Docker Compose

```sh
cp .env.example .env
```

Set `BOT_TOKEN` and `ADMIN_IDS` in `.env`, then:

```sh
chmod 600 .env
docker compose up -d --build
docker compose logs -f bot
```

The Compose setup keeps the SQLite database and downloaded model cache in Docker volumes. To stop the bot:

```sh
docker compose down
```

For local development, create a virtual environment, install `requirements.txt`, configure `.env`, and run `python bot.py`. See the Compose file and `.env.example` for the configuration supported by this checkout.

## Bot commands

| Command | Description |
| --- | --- |
| `/start` | Open the main menu |
| `/help` | Show usage instructions |
| `/stats` | Show usage statistics |
| `/broadcast <text>` | Send an announcement (administrators only) |

## Configuration

- `BOT_TOKEN` — required Telegram bot token
- `ADMIN_IDS` — comma-separated Telegram user IDs with administrator access
- `WHISPER_MODEL` — transcription model; the repository default is `base`
- `WHISPER_BEAM_SIZE` — decoding beam size
- `WHISPER_CPU_THREADS` — CPU thread count; `0` selects automatically

Never commit `.env`. If a real bot token was ever committed or shared, revoke it through BotFather and issue a replacement.

## Notes

Transcription quality and latency vary with the model, language, audio quality, and server hardware. Obtain permission before processing other people's recordings and follow Telegram's rules and applicable privacy requirements.
