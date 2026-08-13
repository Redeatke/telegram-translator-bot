FROM python:3.12-slim

# Install system dependencies: ffmpeg, Node.js, npm, Deno, curl, git
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs npm curl unzip git && \
    curl -fsSL https://deno.land/install.sh | sh && \
    mv /root/.deno/bin/deno /usr/local/bin/deno && \
    rm -rf /var/lib/apt/lists/*

# Limit Deno and Node.js memory to prevent OOM kills on free-tier hosts
ENV DENO_V8_FLAGS="--max-old-space-size=128"
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
    npm install --production 2>&1 && \
    npm run build 2>&1 || echo "WARNING: POT server build failed — bot will run without PO token generation"

COPY . .
RUN chmod +x start.sh

CMD ["bash", "start.sh"]
