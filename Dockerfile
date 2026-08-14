FROM python:3.12-slim

# Install system dependencies: ffmpeg, Node.js, npm, curl, git
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs npm curl unzip git && \
    rm -rf /var/lib/apt/lists/*

# Limit Node.js memory to stay within free-tier host limits
ENV NODE_OPTIONS="--max-old-space-size=128"

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Install yt-dlp pre-release (nightly) for latest YouTube compatibility fixes
RUN pip install --no-cache-dir --upgrade --pre "yt-dlp[default]"

# Clone and build the bgutil PO Token HTTP server
# This provides Proof-of-Origin tokens at 127.0.0.1:4416 that YouTube requires
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider /opt/pot-provider && \
    cd /opt/pot-provider/server && \
    npm install && \
    NODE_OPTIONS="--max-old-space-size=2048" npx tsc && \
    npm prune --production

COPY . .
RUN chmod +x start.sh

CMD ["bash", "start.sh"]
