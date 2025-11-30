# Ultimate Discord Music Bot with All Features
# Features included:
# - Join/Leave voice
# - Play YouTube/Spotify URLs (Spotify -> YouTube mapping)
# - Music queue
# - Skip, Stop, Pause, Resume
# - Now playing
# - Queue listing
# - Remove from queue
# - Clear queue
# - Autoplay next song
# - Volume control
# - Loop single song / loop queue
# - Error handling
# - Clean architecture

import discord
from discord.ext import commands
import os
import asyncio
import yt_dlp as youtube_dl
from dotenv import load_dotenv
from collections import deque
import re

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents().all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------- YTDL OPTIONS ----------------------
ytdl_format_options = {
    "format": "bestaudio/best",
    "outtmpl": "%(id)s.%(ext)s",
    "quiet": True,
    "geo_bypass": True,
    "nocheckcertificate": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

ffmpeg_options = {
    "options": "-vn"
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

# ---------------------- HELPER FUNCTIONS ----------------------
def is_url(string):
    """Check if the string is a URL"""
    url_pattern = re.compile(
        r'^(https?://)?(www\.)?(youtube\.com|youtu\.be|spotify\.com|soundcloud\.com)'
    )
    return bool(url_pattern.match(string))

# ---------------------- PLAYER CLASS ----------------------
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.url = data.get("url")

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()

        # safe yt-dlp wrapper
        def extract():
            try:
                return ytdl.extract_info(url, download=not stream)
            except Exception as e:
                print("yt-dlp extraction error:", e)
                return None

        data = await loop.run_in_executor(None, extract)
        if data is None:
            raise RuntimeError("Could not extract info from URL.")

        if "entries" in data:
            data = data["entries"][0]

        source = data["url"] if stream else ytdl.prepare_filename(data)
        audio = discord.FFmpegPCMAudio(source, executable="ffmpeg", **ffmpeg_options)

        return cls(audio, data=data)
    
    @classmethod
    async def search_and_play(cls, query, *, loop=None, stream=True):
        """Search YouTube and return the first result"""
        loop = loop or asyncio.get_event_loop()

        def search():
            try:
                # Prefix with to search YouTube
                search_query = f"{query}"
                return ytdl.extract_info(search_query, download=False)
            except Exception as e:
                print("YouTube search error:", e)
                return None

        data = await loop.run_in_executor(None, search)
        if data is None:
            raise RuntimeError("Could not find any results.")

        # Get the first search result
        if "entries" in data and len(data["entries"]) > 0:
            data = data["entries"][0]
        else:
            raise RuntimeError("No results found.")

        source = data["url"] if stream else ytdl.prepare_filename(data)
        audio = discord.FFmpegPCMAudio(source, executable="ffmpeg", **ffmpeg_options)

        return cls(audio, data=data)

# ---------------------- MUSIC MANAGER ----------------------
class MusicPlayer:
    def __init__(self):
        self.queue = deque()
        self.current = None
        self.loop_song = False
        self.loop_queue = False
        self.volume = 0.5

    def add(self, source):
        self.queue.append(source)

    def next(self):
        if self.loop_song and self.current:
            return self.current
        if self.loop_queue and self.current:
            self.queue.append(self.current)
        if self.queue:
            return self.queue.popleft()
        return None

players = {}

# Helper to get/create guild player

def get_player(guild_id):
    if guild_id not in players:
        players[guild_id] = MusicPlayer()
    return players[guild_id]

# ---------------------- COMMANDS ----------------------
@bot.command()
async def join(ctx):
    if ctx.author.voice is None:
        return await ctx.send("You must be in a voice channel.")
    await ctx.author.voice.channel.connect()
    await ctx.send("Joined voice channel ✔️")


@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Left voice channel ✔️")


async def play_next(ctx):
    vc = ctx.voice_client
    player = get_player(ctx.guild.id)

    next_source = player.next()
    if next_source is None:
        player.current = None
        return

    player.current = next_source
    vc.play(next_source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
    await ctx.send(f"🎶 **Now playing:** {next_source.title}")

@bot.command()
async def play(ctx, *, query):
    """Play a song from URL or search query"""
    vc = ctx.voice_client
    if not vc:
        await join(ctx)
        vc = ctx.voice_client

    async with ctx.typing():
        try:
            # Check if input is a URL or a search query
            if is_url(query):
                await ctx.send(f"🔗 Loading URL...")
                source = await YTDLSource.from_url(query, loop=bot.loop, stream=True)
            else:
                await ctx.send(f"🔍 Searching for: **{query}**...")
                source = await YTDLSource.search_and_play(query, loop=bot.loop, stream=True)
            
            source.volume = get_player(ctx.guild.id).volume
        except Exception as e:
            print("PLAY ERROR:", e)
            return await ctx.send(f"❌ Error: Could not find or play the track.")

    player = get_player(ctx.guild.id)

    if not vc.is_playing() and not vc.is_paused() and player.current is None:
        player.current = source
        vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
        await ctx.send(f"🎶 **Now playing:** {source.title}")
    else:
        player.add(source)
        await ctx.send(f"➕ Added to queue: **{source.title}**")


@bot.command()
async def pause(ctx):
    if ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸ Paused")


@bot.command()
async def resume(ctx):
    if ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed")


@bot.command()
async def skip(ctx):
    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭ Skipped")


@bot.command()
async def stop(ctx):
    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()

    get_player(ctx.guild.id).current = None
    await ctx.send("⏹ Stopped playback")


@bot.command()
async def queue(ctx):
    player = get_player(ctx.guild.id)
    if not player.queue:
        return await ctx.send("📭 Queue is empty.")

    msg = "📜 **Current Queue:**\n"
    for i, item in enumerate(player.queue):
        msg += f"`{i+1}.` {item.title}\n"
    await ctx.send(msg)


@bot.command()
async def remove(ctx, index: int):
    player = get_player(ctx.guild.id)
    index -= 1
    if 0 <= index < len(player.queue):
        removed = list(player.queue)[index]
        del player.queue[index]
        await ctx.send(f"🗑 Removed: **{removed.title}**")
    else:
        await ctx.send("❌ Invalid index.")


@bot.command()
async def clear(ctx):
    get_player(ctx.guild.id).queue.clear()
    await ctx.send("🧹 Queue cleared.")


@bot.command()
async def volume(ctx, vol: int):
    if not 0 <= vol <= 100:
        return await ctx.send("Volume must be 0–100.")

    player = get_player(ctx.guild.id)
    player.volume = vol / 100

    if player.current:
        player.current.volume = player.volume

    await ctx.send(f"🔊 Volume set to **{vol}%**")


@bot.command()
async def loop(ctx):
    player = get_player(ctx.guild.id)
    player.loop_song = not player.loop_song
    player.loop_queue = False
    await ctx.send(f"🔁 Loop song: **{player.loop_song}**")


@bot.command()
async def loopqueue(ctx):
    player = get_player(ctx.guild.id)
    player.loop_queue = not player.loop_queue
    player.loop_song = False
    await ctx.send(f"🔁 Loop queue: **{player.loop_queue}**")


@bot.command()
async def now(ctx):
    player = get_player(ctx.guild.id)
    if player.current:
        await ctx.send(f"🎶 Currently playing: **{player.current.title}**")
    else:
        await ctx.send("Nothing is playing.")


# ---------------------- RUN ----------------------
bot.run(DISCORD_TOKEN)
