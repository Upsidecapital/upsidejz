# Claude Discord Bot

A Discord bot powered by Anthropic's Claude AI that responds to mentions and DMs with intelligent, context-aware replies.

## Features

- Responds when mentioned (`@BotName`) or via DM
- Maintains conversation history per channel (last 20 messages)
- Auto-splits long responses to respect Discord's 2000-character limit
- Shows typing indicator while generating a response
- `!clear` command to reset conversation history
- `!ping` command to check latency

## Setup

### 1. Create a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** and give it a name
3. Navigate to **Bot** in the sidebar
4. Click **Reset Token** and copy your bot token
5. Under **Privileged Gateway Intents**, enable **Message Content Intent**
6. Go to **OAuth2 → URL Generator**, select the `bot` scope and these permissions:
   - View Channels
   - Send Messages
   - Read Message History
7. Use the generated URL to invite the bot to your server

### 2. Get an Anthropic API Key

Sign up at [console.anthropic.com](https://console.anthropic.com) and create an API key.

### 3. Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your tokens:

```
DISCORD_TOKEN=your_discord_bot_token_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
```

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

### 5. Run the Bot

```bash
python bot.py
```

## Commands

| Command | Description |
|---------|-------------|
| `!ping` | Check bot latency |
| `!clear` | Clear conversation history for the current channel |

## Usage

- **Mention the bot** in any channel: `@BotName What is the capital of France?`
- **DM the bot** directly for private conversations
- The bot remembers context within each channel (up to 20 messages)
