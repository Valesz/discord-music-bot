# bot.py
# Slash-only Discord music bot with buttons UI, auto-leave, safe download cleanup, autocomplete, logging, PLAYLIST SUPPORT

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
from datetime import datetime, timedelta

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

bot = commands.Bot(command_prefix="!", intents=intents)
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
        try:
            loop = asyncio.get_event_loop()
            return loop.create_task(coro)
        except Exception:
            raise RuntimeError("Unable to schedule coroutine; no event loop available")

# ---------------------- YTDL / FFMPEG ----------------------
ytdl_format_options = {
    "format": "bestaudio/best",
    "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
    "quiet": True,
    "no_warnings": True,
    "geo_bypass": True,
    "nocheckcertificate": True,
    "noplaylist": False,  # MÓDOSÍTVA: engedélyezzük a lejátszási listákat
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": "in_playlist",  # KRITIKUS: ne töltse le a teljes playlist infót, csak a video ID-kat
    "ignoreerrors": True,  # folytassa hibák esetén
    "no_color": True,
    "cookiefile": None,  # opcionálisan add hozzá a cookie fájlt ha van
}

ffmpeg_options = {"options": "-vn"}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

URL_RE = re.compile(r'^(https?://)?(www\.)?(youtube\.com|youtu\.be|spotify\.com|soundcloud\.com)')
PLAYLIST_RE = re.compile(r'(youtube\.com/playlist\?|youtube\.com/watch\?.*&list=|youtu\.be/.*\?list=)')

def is_url(s: str) -> bool:
    return bool(URL_RE.match(s))

def is_playlist_url(s: str) -> bool:
    """Ellenőrzi, hogy a megadott URL lejátszási lista-e"""
    return bool(PLAYLIST_RE.search(s))

# ---------------------- YTDL SOURCE (with safe cleanup) ----------------------
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source: discord.AudioSource, *, data: dict, filepath: Optional[str] = None, volume: float = 0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.uploader = data.get("uploader")
        self.webpage_url = data.get("webpage_url")
        self.duration = data.get("duration", 0)
        self.filepath = filepath
        self.start_time: Optional[float] = None
        logger.debug = logger.debug

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

        # Ha keresési eredmény, vedd az első találatot
        if "entries" in data and not data.get("_type") == "playlist":
            data = data["entries"][0]

        filepath = None
        if download:
            filepath = ytdl.prepare_filename(data)
            logger.info(f"Downloaded to: {filepath}")
            audio_source = discord.FFmpegPCMAudio(filepath, executable="ffmpeg", **ffmpeg_options)
        else:
            source_url = data.get("url")
            audio_source = discord.FFmpegPCMAudio(source_url, executable="ffmpeg", **ffmpeg_options)

        return cls(audio_source, data=data, filepath=filepath)

    @classmethod
    async def extract_playlist_info(cls, url: str, *, loop: Optional[asyncio.AbstractEventLoop] = None):
        """
        Lejátszási lista információk kinyerése letöltés nélkül.
        Visszaadja a lejátszási lista címét és a videók listáját.
        Ez a módszer FLAT extraction-t használ, hogy elkerülje a copyright hibákat.
        """
        loop = loop or asyncio.get_event_loop()
        logger.info(f"Extracting playlist info for: {url}")

        def extract():
            try:
                # Flat extraction: csak a video ID-kat szerezzük meg, ne a teljes metaadatokat
                # Ez elkerüli a copyright/restriction hibákat a playlist szintjén
                opts = ytdl_format_options.copy()
                opts['extract_flat'] = 'in_playlist'  # csak alapvető infó
                opts['ignoreerrors'] = True  # hibák esetén folytassa
                
                temp_ytdl = youtube_dl.YoutubeDL(opts)
                return temp_ytdl.extract_info(url, download=False)
            except Exception as e:
                logger.error(f"Playlist extraction error for {url}: {e}")
                return None

        data = await loop.run_in_executor(None, extract)
        if data is None:
            raise RuntimeError("Failed to extract playlist data")

        # Ellenőrizzük, hogy valóban lejátszási lista-e
        if data.get("_type") != "playlist":
            # Ha nem playlist, akkor lehet egy video URL list= paraméterrel
            # Próbáljuk meg egyedi videóként kezelni
            if "entries" not in data:
                raise RuntimeError("URL is not a playlist")
        
        if "entries" not in data:
            raise RuntimeError("Playlist has no entries")

        playlist_title = data.get("title", "Unknown Playlist")
        
        # Szűrjük a None és üres értékeket, és alakítsuk át megfelelő formátumra
        entries = []
        for entry in data["entries"]:
            if entry and entry.get("id"):
                # Ha flat extraction van, akkor csak az ID-t kapjuk
                # Építsük fel a teljes URL-t
                video_id = entry.get("id")
                entry_url = entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
                
                # Készítsünk egy egyszerű entry objektumot
                simple_entry = {
                    "id": video_id,
                    "url": entry_url,
                    "webpage_url": entry_url,
                    "title": entry.get("title", f"Video {video_id}"),
                    "duration": entry.get("duration"),
                }
                entries.append(simple_entry)
        
        logger.info(f"Playlist '{playlist_title}' contains {len(entries)} videos")
        return playlist_title, entries

    def _close_ffmpeg_process(self):
        """Attempt to cleanly close FFmpeg process / pipes used by discord.FFmpegPCMAudio."""
        try:
            if hasattr(self, "cleanup"):
                try:
                    self.cleanup()
                except Exception:
                    pass

            wrapped = getattr(self, "_source", None)
            if wrapped:
                if hasattr(wrapped, "cleanup"):
                    try:
                        wrapped.cleanup()
                    except Exception:
                        pass
                proc = getattr(wrapped, "process", None) or getattr(wrapped, "_process", None)
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass

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
            self._close_ffmpeg_process()
        except Exception as e:
            logger.debug(f"Error closing ffmpeg process: {e}")

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
        self.is_loading_playlist: bool = False  # NEW: flag to track playlist loading
        self.stop_loading: bool = False  # NEW: flag to signal stop
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
        self.stop_loading = True  # NEW: signal to stop any ongoing playlist loading
        tasks = []
        while self.queue:
            item = self.queue.popleft()
            tasks.append(asyncio.create_task(item.async_cleanup()))
        if self.current:
            tasks.append(asyncio.create_task(self.current.async_cleanup()))
            self.current = None
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Reset the flag after a short delay
        await asyncio.sleep(0.5)
        self.stop_loading = False

players: Dict[int, MusicPlayer] = {}

def get_player(guild_id: int) -> MusicPlayer:
    if guild_id not in players:
        players[guild_id] = MusicPlayer(guild_id)
    return players[guild_id]

# ---------------------- PROGRESS BAR HELPER ----------------------
def format_time(seconds: int) -> str:
    """Format seconds into MM:SS or HH:MM:SS format using timedelta."""
    if seconds < 0:
        seconds = 0
    td = timedelta(seconds=seconds)
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

def create_progress_bar(current: int, total: int, length: int = 20) -> str:
    """Create an ASCII progress bar."""
    if total <= 0:
        return f"`[{'─' * length}]` {format_time(current)} / LIVE"
    
    progress = min(current / total, 1.0)
    filled = int(length * progress)
    bar = '━' * filled + '─' * (length - filled)
    
    return f"`[{bar}]` {format_time(current)} / {format_time(total)}"

def get_current_playback_time(player: MusicPlayer, voice_client) -> int:
    """Calculate current playback position in seconds using datetime."""
    if not player.current or not player.current.start_time:
        return 0
    
    now = datetime.now()
    start = datetime.fromtimestamp(player.current.start_time)
    elapsed = (now - start).total_seconds()
    return int(elapsed)

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
        player = get_player(interaction.guild.id)
        
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Resumed", ephemeral=True)
        elif vc and not vc.is_playing() and len(player.queue) > 0:
            # Special case: nothing playing but queue has songs
            await interaction.response.send_message("▶️ Starting playback from queue...", ephemeral=True)
            await _play_next_for_guild(interaction.guild.id)
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

    next_source = player.next()
    prev = player.current
    if next_source is None:
        player.current = None
        logger.info(f"[Guild {guild_id}] Queue ended")
        if text_channel:
            try:
                await text_channel.send("Queue ended.")
            except Exception:
                pass
        if prev:
            safe_create_task(prev.async_cleanup())
        return

    player.current = next_source
    player.current.volume = player.volume
    player.current.start_time = datetime.now().timestamp()
    logger.info(f"[Guild {guild_id}] Now playing: {player.current.title}")

    def _after_play(error):
        if error:
            logger.error(f"[Guild {guild_id}] Playback error: {error}")
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
        if prev:
            safe_create_task(prev.async_cleanup())
        return

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
                if age > 3600:
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
    view = MusicControls(interaction.guild_id)
    await interaction.response.send_message("Joined your voice channel ✅", view=view)

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

def is_video_available(entry: dict) -> tuple[bool, Optional[str]]:
    """
    Check if a video is available for playback.
    Returns (is_available, reason_if_not)
    """
    if not entry:
        return False, "Invalid entry"
    
    # Check if video is unavailable
    if entry.get("unavailable", False):
        return False, "Video unavailable"
    
    # Check if video is private
    if entry.get("is_private", False):
        return False, "Private video"
    
    # Check for age restriction
    if entry.get("age_limit", 0) > 0:
        # We can still try to play age-restricted videos
        pass
    
    # Check for geo-restriction
    availability = entry.get("availability")
    if availability and availability not in ["public", "unlisted", None]:
        return False, f"Restricted ({availability})"
    
    # Check for explicit restriction messages
    if "This video is unavailable" in str(entry.get("title", "")):
        return False, "Video unavailable"
    
    # Check if removed or deleted
    if entry.get("removed", False) or entry.get("deleted", False):
        return False, "Video removed or deleted"
    
    # Check live status (optional: skip live streams if desired)
    # is_live = entry.get("is_live", False)
    # if is_live:
    #     return False, "Live stream"
    
    return True, None

async def safe_extract_video(video_url: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> Optional[YTDLSource]:
    """
    Safely extract and create a YTDLSource, with proper error handling for restricted content.
    Returns None if video cannot be played.
    """
    loop = loop or asyncio.get_event_loop()
    
    def extract_and_download():
        try:
            # Direktben letöltünk és ellenőrzünk
            # Ha bármilyen hiba van, yt-dlp automatikusan elkapja
            opts = ytdl_format_options.copy()
            opts['extract_flat'] = False  # teljes infó kell a lejátszáshoz
            opts['noplaylist'] = True  # biztosan csak egy videó
            opts['ignoreerrors'] = False  # itt már nem ignoráljuk a hibákat
            
            temp_ytdl = youtube_dl.YoutubeDL(opts)
            info = temp_ytdl.extract_info(video_url, download=True)
            
            if not info:
                return None, "Failed to extract info"
            
            # Ha keresési eredmény lenne (nem kellene lennie)
            if "entries" in info:
                info = info["entries"][0]
            
            return info, None
            
        except youtube_dl.utils.DownloadError as e:
            error_msg = str(e).lower()
            if "copyright" in error_msg:
                return None, "Copyright restriction"
            elif "not available" in error_msg or "unavailable" in error_msg:
                return None, "Video unavailable"
            elif "private" in error_msg:
                return None, "Private video"
            elif "deleted" in error_msg or "removed" in error_msg:
                return None, "Video removed"
            elif "country" in error_msg or "geo" in error_msg or "location" in error_msg:
                return None, "Geographic restriction"
            elif "age" in error_msg:
                return None, "Age restriction"
            elif "premieres" in error_msg:
                return None, "Video premiere (not yet available)"
            elif "members" in error_msg or "member" in error_msg:
                return None, "Members-only content"
            else:
                logger.warning(f"Download error for {video_url}: {str(e)[:100]}")
                return None, "Cannot download"
        except youtube_dl.utils.ExtractorError as e:
            error_msg = str(e).lower()
            if "private" in error_msg:
                return None, "Private video"
            elif "unavailable" in error_msg:
                return None, "Video unavailable"
            else:
                logger.warning(f"Extractor error for {video_url}: {str(e)[:100]}")
                return None, "Extraction failed"
        except Exception as e:
            logger.warning(f"Unexpected error for {video_url}: {str(e)[:100]}")
            return None, "Unexpected error"
    
    # Próbáljuk meg letölteni
    info, error_reason = await loop.run_in_executor(None, extract_and_download)
    
    if info is None:
        logger.info(f"Skipping video {video_url}: {error_reason}")
        return None
    
    # Most készítsük el a forrást a letöltött fájlból
    try:
        filepath = ytdl.prepare_filename(info)
        if not Path(filepath).exists():
            logger.error(f"Downloaded file not found: {filepath}")
            return None
        
        audio_source = discord.FFmpegPCMAudio(filepath, executable="ffmpeg", **ffmpeg_options)
        source = YTDLSource(audio_source, data=info, filepath=filepath)
        return source
    except Exception as e:
        logger.error(f"Error creating audio source for {video_url}: {e}")
        return None

@tree.command(name="play", description="Play a song or playlist from a URL or search terms")
@app_commands.describe(query="YouTube URL, playlist URL, or search keywords")
async def play(interaction: Interaction, query: str):
    if interaction.user.voice is None:
        return await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)

    vc = interaction.guild.voice_client
    if vc is None:
        vc = await interaction.user.voice.channel.connect()

    player = get_player(interaction.guild_id)
    player.text_channel_id = interaction.channel_id

    # Ellenőrizzük, hogy lejátszási lista URL-e
    if is_url(query) and is_playlist_url(query):
        await interaction.response.send_message("🔎 Loading playlist (this may take a moment)...", ephemeral=True)
        
        # Set loading flag
        player.is_loading_playlist = True
        player.stop_loading = False
        
        try:
            # Szerezzük meg a lejátszási lista információit
            playlist_title, entries = await YTDLSource.extract_playlist_info(query, loop=bot.loop)
            
            if not entries:
                player.is_loading_playlist = False
                return await interaction.followup.send("❌ Playlist is empty or unavailable.", ephemeral=True)
            
            # Limitáljuk a lejátszási listát (pl. max 50 dal)
            MAX_PLAYLIST_SIZE = 50
            total_videos = len(entries)
            if total_videos > MAX_PLAYLIST_SIZE:
                await interaction.followup.send(
                    f"⚠️ Playlist contains {total_videos} videos. Only the first {MAX_PLAYLIST_SIZE} will be processed.",
                    ephemeral=True
                )
                entries = entries[:MAX_PLAYLIST_SIZE]
            
            # Első dal lejátszása vagy sorba állítása
            added_count = 0
            skipped_count = 0
            first_song = True
            skipped_reasons = {}
            
            for i, entry in enumerate(entries, 1):
                # CHECK: Ha a stop flag be van állítva, állítsuk le a playlist betöltését
                if player.stop_loading:
                    logger.info(f"[Guild {interaction.guild_id}] Playlist loading stopped by user at {i}/{len(entries)}")
                    await interaction.followup.send(
                        f"⏹ Playlist loading stopped. Added **{added_count}** songs before stopping.",
                        ephemeral=True
                    )
                    player.is_loading_playlist = False
                    return
                
                try:
                    # Videó URL készítése a flat extraction eredményéből
                    video_url = entry.get("url") or entry.get("webpage_url")
                    
                    if not video_url:
                        logger.warning(f"No URL for entry {i}, skipping")
                        skipped_count += 1
                        skipped_reasons["No URL"] = skipped_reasons.get("No URL", 0) + 1
                        continue
                    
                    # Biztonságos forrás létrehozása (ez most letölt és ellenőriz)
                    # A safe_extract_video már kezeli az összes lehetséges hibát
                    source = await safe_extract_video(video_url, loop=bot.loop)
                    
                    if source is None:
                        skipped_count += 1
                        skipped_reasons["Restricted or unavailable"] = skipped_reasons.get("Restricted or unavailable", 0) + 1
                        continue
                    
                    source.volume = player.volume
                    
                    # Első dal kezelése
                    if first_song and not vc.is_playing() and not vc.is_paused() and player.current is None:
                        player.current = source
                        player.current.start_time = datetime.now().timestamp()
                        
                        def _after_play(err):
                            if err:
                                logger.error(f"[Guild {interaction.guild_id}] Playback error: {err}")
                            fut = asyncio.run_coroutine_threadsafe(_play_next_for_guild(interaction.guild_id), bot.loop)
                            try:
                                fut.result()
                            except Exception as exc:
                                logger.error(f"Error scheduling next after initial play: {exc}")
                        
                        vc.play(source, after=_after_play)
                        first_song = False
                        
                        # "Now Playing" üzenet
                        view = MusicControls(interaction.guild_id)
                        embed = discord.Embed(
                            title="Now Playing",
                            description=f"**{source.title}**\n\n📋 From playlist: *{playlist_title}*",
                            color=0x1DB954
                        )
                        if source.uploader:
                            embed.set_footer(text=f"From {source.uploader}")
                        await interaction.followup.send(embed=embed, view=view)
                    else:
                        # Sorba állítás
                        player.add(source)
                    
                    added_count += 1
                    logger.info(f"Added song {i}/{len(entries)} from playlist: {source.title}")
                    
                except Exception as e:
                    logger.error(f"Error adding song {i}/{len(entries)} from playlist: {e}")
                    skipped_count += 1
                    skipped_reasons["Error"] = skipped_reasons.get("Error", 0) + 1
                    continue
            
            # Reset loading flag
            player.is_loading_playlist = False
            
            # Részletes összefoglaló üzenet
            if added_count > 0:
                summary = f"✅ Added **{added_count}** songs from playlist **{playlist_title}**"
                if skipped_count > 0:
                    summary += f"\n⚠️ Skipped **{skipped_count}** unavailable videos"
                    if skipped_reasons:
                        reasons_text = ", ".join([f"{count} {reason}" for reason, count in skipped_reasons.items()])
                        summary += f"\n_Reasons: {reasons_text}_"
                await interaction.followup.send(summary, ephemeral=True)
            elif skipped_count > 0:
                summary = f"❌ Could not add any songs from the playlist.\n⚠️ All **{skipped_count}** videos were unavailable or restricted."
                if skipped_reasons:
                    reasons_text = "\n".join([f"• {reason}: {count}" for reason, count in skipped_reasons.items()])
                    summary += f"\n\n**Reasons:**\n{reasons_text}"
                await interaction.followup.send(summary, ephemeral=True)
            else:
                await interaction.followup.send("❌ Could not add any songs from the playlist.", ephemeral=True)
                
        except Exception as e:
            player.is_loading_playlist = False
            logger.error(f"[Guild {interaction.guild_id}] Playlist error for '{query}': {e}")
            return await interaction.followup.send("❌ Could not load playlist.", ephemeral=True)
    
    else:
        # Egyedi dal lejátszása (eredeti logika)
        await interaction.response.send_message("🔎 Loading (this can take a few seconds)...", ephemeral=True)
        
        extract_query = query if is_url(query) else f"ytsearch1:{query}"

        try:
            source = await YTDLSource.from_url(extract_query, loop=bot.loop, download=True)
        except Exception as e:
            logger.error(f"[Guild {interaction.guild_id}] Play extraction error for '{query}': {e}")
            return await interaction.followup.send("❌ Could not find or play that song.", ephemeral=True)

        source.volume = player.volume

        if not vc.is_playing() and not vc.is_paused() and player.current is None:
            player.current = source
            player.current.start_time = datetime.now().timestamp()

            def _after_play(err):
                if err:
                    logger.error(f"[Guild {interaction.guild_id}] Playback error: {err}")
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
        view = MusicControls(interaction.guild_id)
        await interaction.response.send_message("⏭ Skipped", view=view)
    else:
        await interaction.response.send_message("Nothing to skip.", ephemeral=True)

@tree.command(name="pause", description="Pause playback")
async def pause(interaction: Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        view = MusicControls(interaction.guild_id)
        await interaction.response.send_message("⏸ Paused", view=view)
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)

@tree.command(name="resume", description="Resume playback")
async def resume(interaction: Interaction):
    vc = interaction.guild.voice_client
    player = get_player(interaction.guild_id)
    
    if vc and vc.is_paused():
        vc.resume()
        view = MusicControls(interaction.guild_id)
        await interaction.response.send_message("▶️ Resumed", view=view)
    elif vc and not vc.is_playing() and len(player.queue) > 0:
        # Special case: nothing playing but queue has songs
        # This happens when user skips before next song loads
        logger.info(f"[Guild {interaction.guild_id}] Resume triggered with empty current but non-empty queue")
        await interaction.response.send_message("▶️ Starting playback from queue...", ephemeral=True)
        await _play_next_for_guild(interaction.guild_id)
    elif not vc:
        await interaction.response.send_message("Bot is not connected to a voice channel.", ephemeral=True)
    elif len(player.queue) == 0 and player.current is None:
        await interaction.response.send_message("Nothing to resume. Queue is empty.", ephemeral=True)
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
    vc = interaction.guild.voice_client
    
    embed = discord.Embed(title="Queue", color=0x2F3136)
    
    if player.current:
        current_time = get_current_playback_time(player, vc)
        progress = create_progress_bar(current_time, player.current.duration)
        embed.add_field(
            name="🎵 Now Playing",
            value=f"**{player.current.title}**\n{progress}",
            inline=False
        )
    
    if not player.queue:
        if not player.current:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        embed.add_field(name="Up Next", value="*Queue is empty*", inline=False)
    else:
        desc = ""
        for i, item in enumerate(player.queue, start=1):
            duration_str = format_time(item.duration) if item.duration else "Unknown"
            desc += f"`{i}.` {item.title} `[{duration_str}]`\n"
            if len(desc) > 950:
                desc += "... and more"
                break
        embed.add_field(name="Up Next", value=desc, inline=False)
    
    view = MusicControls(interaction.guild_id)
    await interaction.response.send_message(embed=embed, view=view)

@tree.command(name="volume", description="Set playback volume (0-100)")
@app_commands.describe(percent="Volume percent 0-100")
async def volume_cmd(interaction: Interaction, percent: int):
    if not 0 <= percent <= 100:
        return await interaction.response.send_message("Volume must be 0–100.", ephemeral=True)
    player = get_player(interaction.guild_id)
    player.volume = percent / 100
    if player.current:
        player.current.volume = player.volume
    view = MusicControls(interaction.guild_id)
    await interaction.response.send_message(f"🔊 Volume set to **{percent}%**", view=view)

@tree.command(name="now", description="Show now playing")
async def now_cmd(interaction: Interaction):
    player = get_player(interaction.guild_id)
    vc = interaction.guild.voice_client
    
    if player.current:
        current_time = get_current_playback_time(player, vc)
        progress = create_progress_bar(current_time, player.current.duration)
        
        embed = discord.Embed(title="Now Playing", description=f"**{player.current.title}**", color=0x1DB954)
        embed.add_field(name="Progress", value=progress, inline=False)
        
        if player.current.uploader:
            embed.set_footer(text=f"From {player.current.uploader}")
        
        status = "⏸ Paused" if vc and vc.is_paused() else "▶️ Playing"
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Volume", value=f"{int(player.volume * 100)}%", inline=True)
        
        view = MusicControls(interaction.guild_id)
        await interaction.response.send_message(embed=embed, view=view)
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