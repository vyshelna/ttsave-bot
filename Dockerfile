FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# при каждом запуске обновляем yt-dlp до свежей версии (если не вышло — запускаемся со старой)
CMD ["sh", "-c", "pip install --no-cache-dir -U yt-dlp || true; python main.py"]
