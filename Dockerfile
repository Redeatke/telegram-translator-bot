FROM python:3.12-slim

# Install system dependencies: ffmpeg, Node.js (for yt-dlp JS runtime), curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Upgrade yt-dlp to latest nightly for latest YouTube fixes
RUN pip install --no-cache-dir --upgrade --pre "yt-dlp[default]"

COPY . .
RUN chmod +x start.sh

CMD ["bash", "start.sh"]
