# 🚀 Deploying to PythonAnywhere

This guide provides step-by-step instructions to deploy your Telegram Translation Bot to [PythonAnywhere](https://www.pythonanywhere.com/) for free, 24/7 hosting.

---

## 🛠️ Step-by-Step Deployment Guide

### Step 1: Create a PythonAnywhere Account
1. Go to [PythonAnywhere](https://www.pythonanywhere.com/).
2. Click **Pricing & Signup** and choose the **Create a Beginner Account** (Free tier).
3. Choose your username, enter your email, and create a password.

### Step 2: Open a Bash Console
1. Once logged into your dashboard, click on the **Consoles** tab.
2. Under **New console**, click on **Bash**. This opens a command line terminal running in the cloud.

### Step 3: Upload Your Files via the Web Interface
Instead of using command line editors, you can easily upload the files directly from your computer using PythonAnywhere's web dashboard:

1. Click on the **Files** tab at the top of your PythonAnywhere page.
2. In the **Directories** section on the right, type `telegram-translator-bot` in the text box next to "New directory" and click the **New directory** button.
3. Click on the newly created `telegram-translator-bot` folder to open it.
4. On the left side under **Upload a file**, click the button and upload the following files from your local computer:
   - **`bot.py`** (Located at `c:\Users\red\Documents\2026\AG\telegram-translator-bot\bot.py`)
   - **`requirements.txt`** (Located at `c:\Users\red\Documents\2026\AG\telegram-translator-bot\requirements.txt`)
   - **`.env`** (Located at `c:\Users\red\Documents\2026\AG\telegram-translator-bot\.env` — *Note: on Windows, files starting with a dot might be hidden. You can show hidden files, or simply upload it directly*).

*(Once uploaded, you can click on `.env` on PythonAnywhere to view/edit it if you want to update the API keys later).*

### Step 4: Install Dependencies
In the PythonAnywhere Bash console (inside the `telegram-translator-bot` directory), run:
```bash
pip3 install --user -r requirements.txt
```

### Step 5: Start the Bot
Run the bot to verify it starts correctly:
```bash
python3 bot.py
```
You should see:
`[INFO] Telegram Translation Bot is now running!`

Test the bot in Telegram to make sure it responds. If it works, press `Ctrl+C` in the console to stop it.

### Step 6: Keep it Running 24/7 (Free Tier Method)
On PythonAnywhere, **Always-on tasks** and **Scheduled tasks** require a paid plan. However, you can run the bot 24/7 completely free inside a standard **Bash Console**:

1. Open your **Bash Console** (inside the `telegram-translator-bot` directory).
2. Start the bot:
   ```bash
   python3 bot.py
   ```
3. Once you see `[INFO] Telegram Translation Bot is now running!`, you can **close your browser tab and turn off your computer**.
4. The process will continue running on PythonAnywhere's servers!

#### How to manage or restart the bot later:
- If the bot ever stops responding (usually only if PythonAnywhere restarts their servers for monthly maintenance, or if the process encounters an error), just:
  1. Log into PythonAnywhere.
  2. Go to the **Consoles** tab.
  3. Under **Active consoles**, click on your previous `Bash` console.
  4. Run `python3 bot.py` again to start it back up!
- If you want to stop the bot manually:
  1. Open the active console on PythonAnywhere.
  2. Press `Ctrl+C` to stop it.

*Note: PythonAnywhere free accounts require you to click a "Renew" button on your dashboard once every 30 days (they will send you an email reminder) to keep your account active.*
