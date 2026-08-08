FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs npm && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --upgrade yt-dlp

COPY . .

CMD ["python", "bot.py"]
