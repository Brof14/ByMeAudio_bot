FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/cache \
    XDG_CACHE_HOME=/app/cache

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY bot.py ./

RUN groupadd --gid 10001 bymeaudio \
    && useradd --uid 10001 --gid 10001 --no-create-home bymeaudio \
    && mkdir -p /app/data /app/cache \
    && chown -R bymeaudio:bymeaudio /app

USER bymeaudio
CMD ["python", "bot.py"]
