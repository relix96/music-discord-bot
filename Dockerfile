# Discord music bot – Python + FFmpeg
FROM python:3.12-slim

# FFmpeg para reprodução de áudio
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Token via env em runtime: -e DISCORD_BOT_TOKEN=...
# Opcional: -e FFMPEG_PATH=/usr/bin/ffmpeg (já no PATH nesta imagem)
CMD ["python", "main.py"]
