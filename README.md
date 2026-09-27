# Старая часть файла осталась без изменений
### 4. Запуск бота
```bash
python bot.py
```
или с использованием venv:
```bash
./venv/bin/python bot.py
```

### 4.1 Docker / VPS deployment
Если бот запускается на VPS или сервере, удобнее использовать Docker. В этом случае база данных хранится в отдельном volume, чтобы не теряться после перезапуска контейнера.

```bash
# 1) Клонируем репозиторий
cd /root
git clone https://github.com/Brof14/ByMeAudio_bot.git
cd /root/ByMeAudio_bot

# 2) Создаём .env из шаблона
cp .env.example .env
nano .env
```

Заполните файл так:
```ini
BOT_TOKEN=ВАШ_ТОКЕН_БОТА
ADMIN_IDS=ВАШ_TELEGRAM_ID

WHISPER_MODEL=base
WHISPER_BEAM_SIZE=1
WHISPER_CPU_THREADS=0
```

> `GROQ_API_KEY` можно не заполнять — текущая версия бота работает локально через `Faster-Whisper` и не использует Groq.

```bash
chmod 600 .env
docker compose up -d --build
```

Путь к базе данных в контейнере задан как `/app/data/audio_bot.db`, а volume `bot_data` монтируется именно в `/app/data`, поэтому SQLite не теряется при пересборке или перезапуске контейнера.

Для просмотра логов:
```bash
docker compose logs -f bot
```

Для остановки и удаления контейнеров:
```bash
docker compose down
```

---
