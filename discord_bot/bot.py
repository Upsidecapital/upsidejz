import os
import discord
from discord.ext import commands
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Store conversation history per channel
conversation_history: dict[int, list] = {}


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")


@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from the bot itself
    if message.author == bot.user:
        return

    # Only respond when mentioned or in DMs
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions

    if not is_dm and not is_mentioned:
        await bot.process_commands(message)
        return

    # Strip the bot mention from the message content
    content = message.content
    if is_mentioned:
        content = content.replace(f"<@{bot.user.id}>", "").strip()

    if not content:
        await message.reply("Hello! How can I help you?")
        return

    channel_id = message.channel.id
    if channel_id not in conversation_history:
        conversation_history[channel_id] = []

    conversation_history[channel_id].append({"role": "user", "content": content})

    # Keep last 20 messages to avoid token limits
    if len(conversation_history[channel_id]) > 20:
        conversation_history[channel_id] = conversation_history[channel_id][-20:]

    async with message.channel.typing():
        try:
            response = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=1024,
                system="You are a helpful assistant in a Discord server. Be concise and friendly.",
                messages=conversation_history[channel_id],
            )

            reply_text = response.content[0].text

            conversation_history[channel_id].append(
                {"role": "assistant", "content": reply_text}
            )

            # Discord has a 2000 character limit per message
            if len(reply_text) > 2000:
                chunks = [reply_text[i:i+2000] for i in range(0, len(reply_text), 2000)]
                await message.reply(chunks[0])
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)
            else:
                await message.reply(reply_text)

        except Exception as e:
            await message.reply(f"Sorry, I encountered an error: {str(e)}")

    await bot.process_commands(message)


@bot.command(name="clear")
async def clear_history(ctx: commands.Context):
    """Clear the conversation history for this channel."""
    channel_id = ctx.channel.id
    if channel_id in conversation_history:
        conversation_history[channel_id] = []
    await ctx.send("Conversation history cleared!")


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    """Check bot latency."""
    await ctx.send(f"Pong! Latency: {round(bot.latency * 1000)}ms")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
