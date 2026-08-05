# 🌐 Telegram Translation Bot

A feature-rich Telegram bot that auto-translates messages using either a **free engine** (Google Translate) or an **AI engine** (via [OpenRouter](https://openrouter.ai) — access to 100+ models including Gemini, Claude, Llama, and more).

## Features

- 🌍 **Auto-translate** in private chats — just send any text
- 🔀 **Dual engines** — Free (Google Translate) or AI (OpenRouter)
- 🏳️ **Language flags** — visual flag emojis for detected languages
- 📝 **/tr command** — translate in groups by replying to messages
- ⚡ **Smart fallback** — if AI fails, falls back to free engine
- 👮 **Group moderation** — /ban, /promote, /demote for admins
- 📨 **/report** — users can report bugs to bot admins
- 🎨 **Styled card UI** — clean Unicode box-drawing message layout

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure `.env`

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here

# OpenRouter (optional — enables AI engine)
OPENROUTER_API_KEY=sk-or-v1-your_key_here
OPENROUTER_MODEL=google/gemini-2.5-flash
```

Get your OpenRouter key at [openrouter.ai](https://openrouter.ai).

### 3. Run

```bash
python bot.py
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Start bot, see current settings |
| `/tr` | Translate text or reply to a message |
| `/target <code>` | Set target language (e.g. `/target es`) |
| `/engine` | Toggle between AI and Free engines |
| `/status` | View your settings and premium status |
| `/help` | Full command list with language codes |
| `/report <text>` | Report a bug to bot admins |
| `/ban` | Ban a user (admin only) |
| `/promote` | Promote to admin (admin only) |
| `/demote` | Demote an admin (admin only) |

## Supported Models (via OpenRouter)

Set `OPENROUTER_MODEL` in `.env` to any model slug from [openrouter.ai/models](https://openrouter.ai/models):

- `google/gemini-2.5-flash` (default — fast & cheap)
- `anthropic/claude-sonnet-4`
- `meta-llama/llama-4-scout`
- `openai/gpt-4o`
- And 100+ more
