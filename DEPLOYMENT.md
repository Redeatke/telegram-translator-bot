# 🚀 Deploying to Northflank

This guide provides step-by-step instructions to deploy your Telegram Translation & Utility Bot to **[Northflank](https://northflank.com/)** for 24/7, reliable cloud hosting with continuous deployment via GitHub.

---

## 🌟 Why Northflank?

* **Native Dockerfile Support**: Builds our custom container directly with Python 3.12, `ffmpeg`, Node.js, Deno (for YouTube PO tokens), and TrueType vector fonts.
* **Continuous Git Deployment**: Every `git push origin master` automatically triggers a zero-downtime build and redeploy.
* **Persistent Long-Polling**: Runs 24/7 without artificial idle sleep timeouts (unlike Render free tier).
* **Live Streaming Logs**: Instant container logs, resource monitoring (CPU/RAM), and restart controls.

---

## 🛠️ Step-by-Step Deployment Guide

### Step 1: Create a Project in Northflank
1. Log in to [Northflank Dashboard](https://app.northflank.com/).
2. Click **Create Project**.
3. Choose a project name (e.g. `telegram-bot-project`) and select your preferred region (e.g., US or Europe).

---

### Step 2: Create a Deployment Service
1. Inside your Northflank project, click **Create Service** → **Deployment Service**.
2. Service Details:
   - **Service Name**: `telegram-translator-bot`
   - **Service Type**: **Deployment Service** (runs continuously as a background daemon/worker).

---

### Step 3: Configure Build Source (GitHub & Dockerfile)
1. Under **Source**: Select **Version Control (Git)**.
2. Select your repository: `Redeatke/telegram-translator-bot` (or your GitHub account).
3. **Branch**: `master`.
4. **Build Type**: Select **Dockerfile**.
5. **Dockerfile path**: `Dockerfile` (root directory).
6. **Build Context**: `/` (root directory).

> [!NOTE]
> The included `Dockerfile` installs `ffmpeg`, `Node.js`, `Deno`, `fonts-dejavu-core`, and sets up `bgutil-pot-provider` so YouTube media downloads and quote sticker generation work out of the box.

---

### Step 4: Configure Environment Variables

Under **Environment Variables** (or **Secrets**), add the following keys:

| Variable Name | Required | Description / Example |
| :--- | :---: | :--- |
| `TELEGRAM_BOT_TOKEN` | **Yes** | Your Telegram bot token from [@BotFather](https://t.me/BotFather). |
| `ADMIN_USER_ID` | **Recommended** | Your numeric Telegram user ID (enables `/admin` and `/maintenance` control panel). |
| `DOWNLOADS_ENABLED` | Optional | Set to `true` (default) or `false` to toggle media downloader. |
| `LOG_LEVEL` | Optional | Set to `INFO` (or `DEBUG` for troubleshooting). |
| `YOUTUBE_COOKIE` | Optional | Netscape-format YouTube cookies string for HQ/restricted YouTube streams. |

---

### Step 5: Networking & Resources
1. **Ports / Networking**:
   - Because the bot operates via Telegram **Long Polling** (`Application.run_polling()`), **no public HTTP ports need to be exposed**.
   - You can leave public networking disabled (internal worker).
2. **Resources**:
   - **Compute plan**: `Micro` (0.5 vCPU / 512MB RAM) or `Small` is recommended to ensure smooth video processing with `ffmpeg`.

---

### Step 6: Deploy & Verify
1. Click **Deploy Service** (or **Create Service**).
2. Northflank will clone the repository, build the Docker image, and start the bot container.
3. Open the **Logs** tab in Northflank. You should see:
   ```text
   [INFO] Telegram Translation Bot is now running!
   [INFO] Active Admin ID: 123456789
   ```
4. Open Telegram and test:
   - Send `/start` or `/ping` in DM to verify responsiveness.
   - Reply to any message with `/q` to test quote stickers.
   - Run `/admin` or `/maintenance` to test the owner control panel.

---

## 🔄 Updating & Redeploying

### Automatic Redeploys (CI/CD)
Whenever you push code changes to GitHub:
```bash
git add .
git commit -m "feat: new feature"
git push origin master
```
Northflank will detect the commit on `master`, build the new container image, and switch traffic seamlessly without manual intervention.

### Manual Restart / Rebuild
If you ever need to restart or force a fresh container build:
1. Go to your service in Northflank.
2. Click the **Actions** dropdown (top right).
3. Click **Restart Service** or **Rebuild**.

---

## ⚠️ Important Deployment Notes

* **Single Instance Rule**: Never run `python bot.py` locally while the Northflank container is active. Telegram only allows one active long-polling connection per bot token. Running two simultaneously will cause conflict errors (`Conflict: terminated by other getUpdates request`) and duplicate responses.
* **Remote Maintenance Mode**: If you need to pause the bot without logging into Northflank, simply send `/maintenance` in Telegram DM to toggle maintenance mode on or off instantly across all chats.
