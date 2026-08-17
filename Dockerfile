FROM python:3.12-slim

# Install system dependencies: ffmpeg, Node.js, curl, and deno (yt-dlp default JS runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs curl unzip && \
    rm -rf /var/lib/apt/lists/* && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# Verify deno is installed
RUN deno --version

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Upgrade yt-dlp to latest nightly for latest YouTube fixes
RUN pip install --no-cache-dir --upgrade --pre "yt-dlp[default]"

COPY . .
RUN chmod +x start.sh

CMD ["bash", "start.sh"]
