# bot.py
# Slash-only Discord music bot with buttons UI, auto-leave, safe download cleanup, autocomplete, logging

import discord
from discord.ext import commands, tasks
from discord import app_commands, Interaction
import yt_dlp as youtube_dl
import os
import asyncio
from dotenv import load_dotenv
from collections import deque
import re
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, List

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ---------------------- LOGGING SETUP ----------------------
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

log_format = logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger("MusicBot")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_format)
console_handler.setLevel(logging.INFO)

file_handler = RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
file_handler.setFormatter(log_format)
file_handler.setLevel(logging.DEBUG)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

logger.info("=" * 60)
logger.info("Bot starting up...")
logger.info("=" * 60)

# ---------------------- CONFIG / DIRECTORIES ----------------------
DOWNLOAD_DIR = Path("music_downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
logger.info(f"Download directory: {DOWNLOAD_DIR.resolve()}")

# ---------------------- BOT SETUP ----------------------
intents = discord.Intents.default()
intents.message_content = False
intents.voice_states = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)  # prefix not used but required by commands.Bot
tree = bot.tree

# ---------------------- UTILS ----------------------
def safe_create_task(coro: asyncio.coroutines) -> asyncio.Task:
    """
    Schedule a coroutine on the bot's main loop in a thread-safe way.
    Use this from synchronous worker threads (e.g. audio after callbacks).
    """
    try:
        return bot.loop.create_task(coro)
    except Exception:
        # Fallback: try getting a running loop (rare case)
        try:
            loop = asyncio.get_event_loop()
            return loop.create_task(coro)
        except Exception:
            # As last resort, run it in a new loop in a background thread (shouldn't be necessary)
            # but we prefer explicit error rather than silently swallowing.
            raise RuntimeError("Unable to schedule coroutine; no event loop available")

# ---------------------- YTDL / FFMPEG ----------------------
ytdl_format_options = {
    "format": "bestaudio/best",
    "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
    "quiet": True,
    "no_warnings": True,
    "geo_bypass": True,
    "nocheckcertificate": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

ffmpeg_options = {"options": "-vn"}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

URL_RE = re.compile(r'^(https?://)?(www\.)?(youtube\.com|youtu\.be|spotify\.com|soundcloud\.com)')

def is_url(s: str) -> bool:
    return bool(URL_RE.match(s))

# ---------------------- YTDL SOURCE (with safe cleanup) ----------------------
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source: discord.AudioSource, *, data: dict, filepath: Optional[str] = None, volume: float = 0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.uploader = data.get("uploader")
        self.webpage_url = data.get("webpage_url")
        self.filepath = filepath  # local file path if downloaded; None when streaming
        logger.debug = logger.debug  # keep linter happy

    @classmethod
    async def from_url(cls, query: str, *, loop: Optional[asyncio.AbstractEventLoop] = None, download: bool = True):
        """Extract info and optionally download. Returns YTDLSource with local filepath (if downloaded)."""
        loop = loop or asyncio.get_event_loop()
        logger.info(f"Extracting info for: {query} (download={download})")

        def extract():
            try:
                return ytdl.extract_info(query, download=download)
            except Exception as e:
                logger.error(f"yt-dlp extraction error for {query}: {e}")
                return None

        data = await loop.run_in_executor(None, extract)
        if data is None:
            raise RuntimeError("yt-dlp failed to extract data")

        # If it's a search or playlist, take the first entry
        if "entries" in data:
            data = data["entries"][0]

        filepath = None
        if download:
            filepath = ytdl.prepare_filename(data)
            logger.info(f"Downloaded to: {filepath}")
            audio_source = discord.FFmpegPCMAudio(filepath, executable="ffmpeg", **ffmpeg_options)
        else:
            # streaming (direct url)
            source_url = data.get("url")
            audio_source = discord.FFmpegPCMAudio(source_url, executable="ffmpeg", **ffmpeg_options)

        return cls(audio_source, data=data, filepath=filepath)

    def _close_ffmpeg_process(self):
        """Attempt to cleanly close FFmpeg process / pipes used by discord.FFmpegPCMAudio."""
        try:
            # discord.FFmpegPCMAudio has a cleanup() method
            if hasattr(self, "cleanup"):
                # If this instance is itself a wrapped AudioSource, call its cleanup
                try:
                    self.cleanup()
                except Exception:
                    # attribute may belong to inner source
                    pass

            # The wrapped AudioSource object is accessible via ._source in some wrappers; handle common cases:
            wrapped = getattr(self, "_source", None)
            if wrapped:
                if hasattr(wrapped, "cleanup"):
                    try:
                        wrapped.cleanup()
                    except Exception:
                        pass
                # kill process if accessible
                proc = getattr(wrapped, "process", None) or getattr(wrapped, "_process", None)
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            # also try to find process attribute on self
            proc2 = getattr(self, "process", None) or getattr(self, "_process", None)
            if proc2:
                try:
                    proc2.kill()
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Exception while closing ffmpeg: {e}")

    async def async_cleanup(self, *, wait: float = 0.2):
        """Async-safe cleanup: close ffmpeg handles, wait a bit, then delete the downloaded file if present."""
        try:
            # Close/kill ffmpeg resources first
            self._close_ffmpeg_process()
        except Exception as e:
            logger.debug(f"Error closing ffmpeg process: {e}")

        # small delay to ensure OS releases the file handle (important on Windows)
        try:
            await asyncio.sleep(wait)
        except Exception:
            pass

        if self.filepath:
            try:
                p = Path(self.filepath)
                if p.exists():
                    p.unlink()
                    logger.info(f"Deleted downloaded file: {self.filepath}")
            except Exception as e:
                logger.error(f"Failed to delete file {self.filepath}: {e}")

# ---------------------- MUSIC PLAYER (per guild) ----------------------
class MusicPlayer:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: deque[YTDLSource] = deque()
        self.current: Optional[YTDLSource] = None
        self.volume: float = 0.5
        self.text_channel_id: Optional[int] = None
        logger.info(f"Created MusicPlayer for guild {guild_id}")

    def add(self, source: YTDLSource):
        self.queue.append(source)
        logger.info(f"[Guild {self.guild_id}] Queued: {source.title} (queue size {len(self.queue)})")

    def next(self) -> Optional[YTDLSource]:
        """Get next track. Note: doesn't perform cleanup here."""
        return self.queue.popleft() if self.queue else None

    async def clear_queue(self):
        """Clear queue and schedule async cleanup for all queued items and current."""
        logger.info(f"[Guild {self.guild_id}] Clearing queue ({len(self.queue)} items)")
        tasks = []
        while self.queue:
            item = self.queue.popleft()
            # we're inside an async function, so it's fine to create_task here
            tasks.append(asyncio.create_task(item.async_cleanup()))
        # cleanup current if any
        if self.current:
            tasks.append(asyncio.create_task(self.current.async_cleanup()))
            self.current = None
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

players: Dict[int, MusicPlayer] = {}

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
        player = get_player(interaction.guild.id)
        if vc and (vc.is_playing() or vc.is_paused()):
            logger.info(f"[Guild {interaction.guild.id}] Skip requested by {interaction.user}")
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
            await player.clear_queue()
            await interaction.response.send_message("⏹ Stopped and cleared queue.", ephemeral=True)
        else:
            await interaction.response.send_message("Not connected.", ephemeral=True)

# ---------------------- AUTOCOMPLETE HELPER ----------------------
async def yt_autocomplete(current: str) -> List[app_commands.Choice[str]]:
    if not current.strip():
        return []
    loop = asyncio.get_event_loop()

    def do_search():
        try:
            return ytdl.extract_info(f"ytsearch5:{current}", download=False)
        except Exception:
            return None

    data = await loop.run_in_executor(None, do_search)
    if not data or "entries" not in data:
        return []
    results = data["entries"][:5]
    choices: List[app_commands.Choice[str]] = []
    for track in results:
        title = track.get("title", "Unknown")
        url = track.get("webpage_url") or track.get("url") or title
        choices.append(app_commands.Choice(name=title[:100], value=url))
    return choices

# ---------------------- PLAYBACK HELPERS ----------------------
async def _play_next_for_guild(guild_id: int):
    """Advance to next track for a guild, scheduling cleanup of previous track after playback ends."""
    player = get_player(guild_id)
    guild = bot.get_guild(guild_id)
    if guild is None:
        logger.warning(f"Guild {guild_id} not found when advancing")
        return
    vc = guild.voice_client
    text_channel = None
    if player.text_channel_id:
        text_channel = bot.get_channel(player.text_channel_id)

    # pick next track
    next_source = player.next()
    prev = player.current  # hold previous to cleanup later
    if next_source is None:
        player.current = None
        logger.info(f"[Guild {guild_id}] Queue ended")
        if text_channel:
            try:
                await text_channel.send("Queue ended.")
            except Exception:
                pass
        # cleanup previous if existed
        if prev:
            # schedule previous cleanup on bot loop
            safe_create_task(prev.async_cleanup())
        return

    # set current to next and play
    player.current = next_source
    player.current.volume = player.volume
    logger.info(f"[Guild {guild_id}] Now playing: {player.current.title}")

    def _after_play(error):
        if error:
            logger.error(f"[Guild {guild_id}] Playback error: {error}")
        # schedule cleanup of prev and advance to next
        if prev:
            safe_create_task(prev.async_cleanup())
        fut = asyncio.run_coroutine_threadsafe(_play_next_for_guild(guild_id), bot.loop)
        try:
            fut.result()
        except Exception as exc:
            logger.error(f"[Guild {guild_id}] Error scheduling next track: {exc}")

    if vc is None:
        logger.warning(f"[Guild {guild_id}] Voice client None when trying to play")
        return

    try:
        vc.play(player.current, after=_after_play)
    except Exception as e:
        logger.error(f"[Guild {guild_id}] Error calling vc.play: {e}")
        # schedule cleanup of prev if present
        if prev:
            safe_create_task(prev.async_cleanup())
        return

    # announce
    if text_channel:
        try:
            view = MusicControls(guild_id)
            embed = discord.Embed(title="Now Playing", description=f"**{player.current.title}**", color=0x1DB954)
            if player.current.uploader:
                embed.set_footer(text=f"Requested from {player.current.uploader}")
            await text_channel.send(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error sending now playing message: {e}")

# ---------------------- AUTO LEAVE TASK ----------------------
@tasks.loop(minutes=1)
async def auto_leave_task():
    for vc in bot.voice_clients:
        try:
            if vc.channel and len(vc.channel.members) == 1 and not vc.is_playing() and not vc.is_paused():
                logger.info(f"[Guild {vc.guild.id}] Auto-leaving {vc.channel.name} (alone)")
                player = get_player(vc.guild.id)
                await player.clear_queue()
                try:
                    await vc.disconnect()
                except Exception as e:
                    logger.error(f"Error disconnecting during auto-leave: {e}")
        except Exception as e:
            logger.error(f"Auto-leave error: {e}")

# ---------------------- ORPHANED FILE CLEANUP TASK ----------------------
@tasks.loop(minutes=10)
async def cleanup_orphaned_files():
    """Remove old files from the download folder (older than 1 hour)."""
    try:
        now = asyncio.get_event_loop().time()
        removed = 0
        for file in DOWNLOAD_DIR.iterdir():
            try:
                if not file.is_file():
                    continue
                age = now - file.stat().st_mtime
                if age > 1300:  # older than 1 hour
                    try:
                        file.unlink()
                        removed += 1
                        logger.debug(f"Removed orphaned file: {file}")
                    except Exception as e:
                        logger.error(f"Failed to remove orphaned file {file}: {e}")
            except Exception as e:
                logger.error(f"Error checking {file}: {e}")
        if removed:
            logger.info(f"Orphaned file cleanup removed {removed} files")
    except Exception as e:
        logger.error(f"Error in orphan cleanup: {e}")

# ---------------------- SLASH COMMANDS ----------------------
@tree.command(name="join", description="Make the bot join your voice channel")
async def join(interaction: Interaction):
    if interaction.user.voice is None:
        return await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
    channel = interaction.user.voice.channel
    await channel.connect()
    await interaction.response.send_message("Joined your voice channel ✅", ephemeral=True)

@tree.command(name="leave", description="Disconnect bot from voice channel")
async def leave(interaction: Interaction):
    vc = interaction.guild.voice_client
    if vc:
        player = get_player(interaction.guild_id)
        await player.clear_queue()
        await vc.disconnect()
        await interaction.response.send_message("Disconnected ✅", ephemeral=True)
    else:
        await interaction.response.send_message("Bot is not connected.", ephemeral=True)

@tree.command(name="play", description="Play a song from a URL or search terms")
@app_commands.describe(query="YouTube URL or search keywords")
async def play(interaction: Interaction, query: str):
    if interaction.user.voice is None:
        return await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)

    vc = interaction.guild.voice_client
    if vc is None:
        vc = await interaction.user.voice.channel.connect()

    player = get_player(interaction.guild_id)
    player.text_channel_id = interaction.channel_id

    await interaction.response.send_message("🔎 Loading (this can take a few seconds)...", ephemeral=True)

    # Build extract query (autocomplete may send a video URL)
    extract_query = query if is_url(query) else f"ytsearch1:{query}"

    try:
        # We download to local file so we can safely cleanup after playing
        source = await YTDLSource.from_url(extract_query, loop=bot.loop, download=True)
    except Exception as e:
        logger.error(f"[Guild {interaction.guild_id}] Play extraction error for '{query}': {e}")
        return await interaction.followup.send("❌ Could not find or play that song.", ephemeral=True)

    source.volume = player.volume

    if not vc.is_playing() and not vc.is_paused() and player.current is None:
        player.current = source

        def _after_play(err):
            if err:
                logger.error(f"[Guild {interaction.guild_id}] Playback error: {err}")
            # schedule next track safely
            fut = asyncio.run_coroutine_threadsafe(_play_next_for_guild(interaction.guild_id), bot.loop)
            try:
                fut.result()
            except Exception as exc:
                logger.error(f"Error scheduling next after initial play: {exc}")

        try:
            vc.play(source, after=_after_play)
        except Exception as e:
            logger.error(f"Error starting playback: {e}")
            await interaction.followup.send("❌ Failed to play audio.", ephemeral=True)
            # cleanup the source file since we couldn't play it
            await source.async_cleanup()
            return

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
        await player.clear_queue()
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
async def volume_cmd(interaction: Interaction, percent: int):
    if not 0 <= percent <= 100:
        return await interaction.response.send_message("Volume must be 0–100.", ephemeral=True)
    player = get_player(interaction.guild_id)
    player.volume = percent / 100
    if player.current:
        player.current.volume = player.volume
    await interaction.response.send_message(f"🔊 Volume set to **{percent}%**", ephemeral=True)

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
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await tree.sync()
        logger.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        logger.error(f"Command sync error: {e}")
    auto_leave_task.start()
    cleanup_orphaned_files.start()
    logger.info("Background tasks started")
    logger.info("Bot is ready!")

@bot.event
async def on_guild_remove(guild):
    logger.info(f"Removed from guild: {guild.name} (ID: {guild.id})")
    if guild.id in players:
        logger.info(f"Cleaning up player for guild {guild.id}")
        await players[guild.id].clear_queue()
        del players[guild.id]

# ---------------------- ERROR HANDLING ----------------------
@bot.event
async def on_error(event, *args, **kwargs):
    logger.exception(f"Unhandled error in event {event}")

# ---------------------- RUN ----------------------
try:
    logger.info("Starting bot...")
    bot.run(TOKEN)
except KeyboardInterrupt:
    logger.info("Bot stopped by user")
except Exception as e:
    logger.critical(f"Fatal error: {e}", exc_info=True)