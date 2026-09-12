FROM python:3.12-slim

# Install system dependencies: ffmpeg, Node.js, curl, git, and deno (yt-dlp default JS runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs npm git curl unzip && \
    rm -rf /var/lib/apt/lists/* && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# Verify deno is installed
RUN deno --version

# Install the bgutil PO Token provider so yt-dlp can generate the PO tokens YouTube
# now requires for most formats. Without this, YouTube serves a hard bot-check wall
# ("Sign in to confirm you're not a bot") to datacenter IPs like Render's, regardless
# of how fresh the cookies are. Cloned to yt-dlp's default script-mode search path.
RUN git clone --single-branch --branch 2.0.0 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /root/bgutil-ytdlp-pot-provider && \
    cd /root/bgutil-ytdlp-pot-provider/server && \
    npm ci && \
    npx tsc

# Prime Deno's and Node's compile/module caches by running the provider script
# once at build time (generous time budget here vs. none at request time), so
# the first real request on a slow/throttled host isn't paying a cold-start
# TS-compile cost against the plugin's internal timeout.
RUN cd /root/bgutil-ytdlp-pot-provider/server && \
    DENO_NO_UPDATE_CHECK=1 DENO_NO_PROMPT=1 deno run \
      --allow-env --allow-net \
      --allow-ffi=/root/bgutil-ytdlp-pot-provider/server/node_modules \
      --allow-write=/root/.cache/bgutil-ytdlp-pot-provider \
      --allow-read=/root/.cache/bgutil-ytdlp-pot-provider,/root/bgutil-ytdlp-pot-provider/server/node_modules \
      src/generate_once.ts --version && \
    node build/generate_once.js --version

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Upgrade yt-dlp to latest nightly for latest YouTube fixes
RUN pip install --no-cache-dir --upgrade --pre "yt-dlp[default]"

COPY . .
RUN chmod +x start.sh

CMD ["bash", "start.sh"]
