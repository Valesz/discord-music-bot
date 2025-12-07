# slash_music_bot.py
# Slash-only Discord music bot with buttons UI and auto-leave

import discord
from discord.ext import commands, tasks
from discord import app_commands, Interaction
import os
import asyncio
import yt_dlp as youtube_dl
from dotenv import load_dotenv
from collections import deque
import re

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)  # prefix is unused, but required for commands.Bot
tree = bot.tree

# ---------------------- YTDL / FFMPEG ----------------------
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

ffmpeg_options = {"options": "-vn"}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

URL_RE = re.compile(r'^(https?://)?(www\.)?(youtube\.com|youtu\.be|spotify\.com|soundcloud\.com)')

def is_url(string: str) -> bool:
    return bool(URL_RE.match(string))

# ---------------------- YTDL SOURCE ----------------------
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.uploader = data.get("uploader")
        self.webpage_url = data.get("webpage_url")

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()

        def extract():
            try:
                return ytdl.extract_info(url, download=not stream)
            except Exception as e:
                # print for debugging
                print("yt-dlp extraction error:", e)
                return None

        data = await loop.run_in_executor(None, extract)
        if data is None:
            raise RuntimeError("Could not extract info from that URL/search.")

        # handle playlists / search results
        if "entries" in data:
            # take first entry
            data = data["entries"][0]

        source_url = data["url"] if stream else ytdl.prepare_filename(data)
        audio = discord.FFmpegPCMAudio(source_url, executable="ffmpeg", **ffmpeg_options)
        return cls(audio, data=data)

# ---------------------- MUSIC PLAYER (per-guild) ----------------------
class MusicPlayer:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue = deque()
        self.current: YTDLSource | None = None
        self.loop_song = False
        self.loop_queue = False
        self.volume = 0.5
        self.text_channel_id: int | None = None  # channel where bot will post updates

    def add(self, source: YTDLSource):
        self.queue.append(source)

    def next(self):
        if self.loop_song and self.current:
            return self.current
        if self.loop_queue and self.current:
            self.queue.append(self.current)
        return self.queue.popleft() if self.queue else None

players: dict[int, MusicPlayer] = {}

def get_player(guild_id: int) -> MusicPlayer:
    if guild_id not in players:
        players[guild_id] = MusicPlayer(guild_id)
    return players[guild_id]

# ---------------------- BUTTONS UI ----------------------
class MusicControls(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    async def _get_vc(self, interaction: Interaction):
        return interaction.guild.voice_client

    @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.gray)
    async def pause(self, interaction: Interaction, button: discord.ui.Button):
        vc = await self._get_vc(interaction)
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸ Paused", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="▶️ Resume", style=discord.ButtonStyle.green)
    async def resume(self, interaction: Interaction, button: discord.ui.Button):
        vc = await self._get_vc(interaction)
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Resumed", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to resume.", ephemeral=True)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.blurple)
    async def skip(self, interaction: Interaction, button: discord.ui.Button):
        vc = await self._get_vc(interaction)
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await interaction.response.send_message("⏭ Skipped", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.red)
    async def stop(self, interaction: Interaction, button: discord.ui.Button):
        vc = await self._get_vc(interaction)
        player = get_player(interaction.guild.id)
        if vc:
            vc.stop()
            player.current = None
            player.queue.clear()
            await interaction.response.send_message("⏹ Stopped and cleared queue.", ephemeral=True)
        else:
            await interaction.response.send_message("Not connected.", ephemeral=True)

    @discord.ui.button(label="🔁 Toggle Loop", style=discord.ButtonStyle.secondary)
    async def toggle_loop(self, interaction: Interaction, button: discord.ui.Button):
        player = get_player(interaction.guild.id)
        player.loop_song = not player.loop_song
        # If enabling song loop, ensure queue-loop is off
        if player.loop_song:
            player.loop_queue = False
        await interaction.response.send_message(f"🔁 Loop song set to **{player.loop_song}**", ephemeral=True)

# ---------------------- PLAYBACK HELPERS ----------------------
async def _play_next_for_guild(guild_id: int):
    """Coroutine that advances to the next track for a guild."""
    player = get_player(guild_id)
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    vc = guild.voice_client
    text_channel = None
    if player.text_channel_id:
        text_channel = bot.get_channel(player.text_channel_id)

    next_source = player.next()
    if next_source is None:
        player.current = None
        # no next track; optionally send a small message
        if text_channel:
            try:
                await text_channel.send("Queue ended.")
            except Exception:
                pass
        return

    player.current = next_source
    # set volume
    player.current.volume = player.volume
    # Play and set callback to call this coroutine again when finished
    def _after_play(error):
        if error:
            print("Player error:", error)
        # schedule next track in event loop
        fut = asyncio.run_coroutine_threadsafe(_play_next_for_guild(guild_id), bot.loop)
        try:
            fut.result()
        except Exception as exc:
            print("Error scheduling next track:", exc)

    if vc is None:
        # voice client disappeared
        return

    vc.play(player.current, after=_after_play)
    # announce
    if text_channel:
        try:
            # include buttons UI
            view = MusicControls(guild_id)
            embed = discord.Embed(title="Now Playing", description=f"**{player.current.title}**", color=0x1DB954)
            if player.current.uploader:
                embed.set_footer(text=f"Requested from {player.current.uploader}")
            await text_channel.send(embed=embed, view=view)
        except Exception:
            pass

# ---------------------- AUTO LEAVE TASK ----------------------
@tasks.loop(minutes=1)
async def auto_leave_task():
    for vc in bot.voice_clients:
        try:
            # if bot's vc is in a channel and it's alone (only bot)
            if vc.channel and len(vc.channel.members) == 1:
                # Also ensure we are not currently playing
                if not vc.is_playing() and not vc.is_paused():
                    await vc.disconnect()
        except Exception as e:
            print("auto_leave error:", e)

# ---------------------- SLASH COMMANDS ----------------------
@tree.command(name="join", description="Make the bot join your voice channel")
async def join(interaction: Interaction):
    if interaction.user.voice is None:
        return await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
    await interaction.user.voice.channel.connect()
    await interaction.response.send_message("Joined your voice channel ✅", ephemeral=True)

@tree.command(name="leave", description="Disconnect bot from voice channel")
async def leave(interaction: Interaction):
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("Disconnected ✅", ephemeral=True)
    else:
        await interaction.response.send_message("Bot is not connected.", ephemeral=True)

@tree.command(name="play", description="Play a song from a URL or search terms")
@app_commands.describe(query="YouTube URL or search keywords")
async def play(interaction: Interaction, query: str):
    # ensure user in a voice channel
    if interaction.user.voice is None:
        return await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)

    # try to connect the bot if not connected
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await interaction.user.voice.channel.connect()

    player = get_player(interaction.guild_id)
    player.text_channel_id = interaction.channel_id

    await interaction.response.send_message("🔎 Loading... (this can take a few seconds)", ephemeral=True)

    # build query: if not a URL, use ytsearch1:
    extract_query = query
    if not is_url(query):
        extract_query = query

    try:
        source = await YTDLSource.from_url(extract_query, loop=bot.loop, stream=True)
    except Exception as e:
        print("Play extraction error:", e)
        return await interaction.followup.send("❌ Could not find or play that song.", ephemeral=True)

    source.volume = player.volume

    # play now or queue
    if not vc.is_playing() and (vc.is_paused() is False) and player.current is None:
        player.current = source
        # play and attach after callback
        def _after(error):
            if error:
                print("Playback error:", error)
            fut = asyncio.run_coroutine_threadsafe(_play_next_for_guild(interaction.guild_id), bot.loop)
            try:
                fut.result()
            except Exception as exc:
                print("Error scheduling next:", exc)

        vc.play(source, after=_after)
        # send Now Playing with buttons
        view = MusicControls(interaction.guild_id)
        embed = discord.Embed(title="Now Playing", description=f"**{source.title}**", color=0x1DB954)
        if source.uploader:
            embed.set_footer(text=f"From {source.uploader}")
        await interaction.followup.send(embed=embed, view=view)
    else:
        player.add(source)
        await interaction.followup.send(f"➕ Queued **{source.title}**", ephemeral=True)

@tree.command(name="skip", description="Skip current track")
async def skip(interaction: Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("⏭ Skipped", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing to skip.", ephemeral=True)

@tree.command(name="pause", description="Pause playback")
async def pause(interaction: Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸ Paused", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)

@tree.command(name="resume", description="Resume playback")
async def resume(interaction: Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Resumed", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing to resume.", ephemeral=True)

@tree.command(name="stop", description="Stop playback and clear queue")
async def stop(interaction: Interaction):
    vc = interaction.guild.voice_client
    player = get_player(interaction.guild_id)
    if vc:
        vc.stop()
        player.current = None
        player.queue.clear()
        await interaction.response.send_message("⏹ Stopped and cleared queue.", ephemeral=True)
    else:
        await interaction.response.send_message("Bot is not connected.", ephemeral=True)

@tree.command(name="queue", description="Show current queue")
async def show_queue(interaction: Interaction):
    player = get_player(interaction.guild_id)
    if not player.queue:
        return await interaction.response.send_message("Queue is empty.", ephemeral=True)
    embed = discord.Embed(title="Queue", color=0x2F3136)
    desc = ""
    for i, item in enumerate(player.queue, start=1):
        desc += f"`{i}.` {item.title}\n"
        if len(desc) > 1900:
            break
    embed.description = desc
    await interaction.response.send_message(embed=embed)

@tree.command(name="volume", description="Set playback volume (0-100)")
@app_commands.describe(percent="Volume percent 0-100")
async def volume(interaction: Interaction, percent: int):
    if not 0 <= percent <= 100:
        return await interaction.response.send_message("Volume must be 0–100.", ephemeral=True)
    player = get_player(interaction.guild_id)
    player.volume = percent / 100
    if player.current:
        player.current.volume = player.volume
    await interaction.response.send_message(f"🔊 Volume set to **{percent}%**", ephemeral=True)

@tree.command(name="loop", description="Toggle looping of current song")
async def loop_cmd(interaction: Interaction):
    player = get_player(interaction.guild_id)
    player.loop_song = not player.loop_song
    if player.loop_song:
        player.loop_queue = False
    await interaction.response.send_message(f"🔁 Loop song: **{player.loop_song}**", ephemeral=True)

@tree.command(name="loopqueue", description="Toggle looping of the queue")
async def loopqueue_cmd(interaction: Interaction):
    player = get_player(interaction.guild_id)
    player.loop_queue = not player.loop_queue
    if player.loop_queue:
        player.loop_song = False
    await interaction.response.send_message(f"🔁 Loop queue: **{player.loop_queue}**", ephemeral=True)

@tree.command(name="now", description="Show now playing")
async def now_cmd(interaction: Interaction):
    player = get_player(interaction.guild_id)
    if player.current:
        embed = discord.Embed(title="Now Playing", description=f"**{player.current.title}**", color=0x1DB954)
        if player.current.uploader:
            embed.set_footer(text=f"From {player.current.uploader}")
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)

# ---------------------- EVENTS / STARTUP ----------------------
@bot.event
async def on_ready():
    # sync application commands
    try:
        await tree.sync()
    except Exception as e:
        print("Command sync error:", e)
    auto_leave_task.start()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ---------------------- RUN ----------------------
bot.run(TOKEN)
