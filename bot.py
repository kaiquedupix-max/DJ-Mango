from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from youtube_autoplay import (
    cleanup_guild_cache,
    cleanup_stale_cache,
    delete_cached_file,
    download_audio_sync,
    recommend_track_sync,
)

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("dj-mango")

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"


def default_volume() -> float:
    try:
        value = int(os.getenv("DEFAULT_VOLUME", "50"))
    except ValueError:
        value = 50
    return max(0, min(value, 100)) / 100


def idle_timeout() -> int:
    try:
        value = int(os.getenv("IDLE_TIMEOUT", "120"))
    except ValueError:
        value = 120
    return max(30, value)


def ydl_options() -> dict:
    options = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": False,
        "source_address": "0.0.0.0",
    }

    cookies = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if cookies:
        options["cookiefile"] = cookies

    return options


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    duration: Optional[int]
    uploader: Optional[str]
    requester_name: str
    text_channel_id: int
    video_id: Optional[str] = None
    thumbnail: Optional[str] = None
    local_path: Optional[str] = None
    autoplay_generated: bool = False


class UserFacingError(Exception):
    pass


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "ao vivo/desconhecida"

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"


def first_entry(info: dict) -> dict:
    entries = info.get("entries")
    if entries is None:
        return info

    for entry in entries:
        if entry:
            return entry

    raise UserFacingError("Nenhum resultado foi encontrado no YouTube.")


def resolve_track_sync(
    query: str,
    requester_name: str,
    text_channel_id: int,
) -> Track:
    query = query.strip()
    target = query if URL_RE.match(query) else f"ytsearch1:{query}"

    try:
        with YoutubeDL(ydl_options()) as ydl:
            info = ydl.extract_info(target, download=False)
    except DownloadError as exc:
        logger.warning("Falha do yt-dlp para %r: %s", query, exc)
        raise UserFacingError(
            "Não consegui encontrar ou acessar essa música no YouTube."
        ) from exc

    if not info:
        raise UserFacingError("Nenhum resultado foi encontrado no YouTube.")

    entry = first_entry(info)
    webpage_url = (
        entry.get("webpage_url")
        or entry.get("original_url")
        or entry.get("url")
    )

    if not webpage_url:
        raise UserFacingError("O YouTube não retornou um link reproduzível.")

    duration = entry.get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    return Track(
        title=str(entry.get("title") or "Música sem título"),
        webpage_url=str(webpage_url),
        duration=duration,
        uploader=entry.get("uploader") or entry.get("channel"),
        requester_name=requester_name,
        text_channel_id=text_channel_id,
        video_id=entry.get("id"),
        thumbnail=entry.get("thumbnail"),
    )


def stream_url_sync(webpage_url: str) -> str:
    try:
        with YoutubeDL(ydl_options()) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
    except DownloadError as exc:
        logger.warning("Falha ao obter stream de %s: %s", webpage_url, exc)
        raise UserFacingError(
            "Não consegui abrir o áudio dessa música. Tente novamente."
        ) from exc

    if not info:
        raise UserFacingError("O YouTube não retornou o áudio da música.")

    entry = first_entry(info)
    stream_url = entry.get("url")

    if not stream_url:
        raise UserFacingError("O YouTube não retornou um stream de áudio válido.")

    return str(stream_url)


class GuildPlayer:
    AUTOPLAY_LABELS = {
        "off": "Desligado",
        "similar": "Mesmo estilo",
        "artist": "Mesmo artista",
    }

    def __init__(self, bot: "MangoBot", guild: discord.Guild) -> None:
        self.bot = bot
        self.guild = guild
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.volume = default_volume()
        self.autoplay_mode = "off"
        self.lock = asyncio.Lock()
        self.closed = False
        self.idle_task: Optional[asyncio.Task] = None
        self.prefetch_task: Optional[asyncio.Task] = None
        self.panel_channel_id: Optional[int] = None
        self.panel_message_id: Optional[int] = None
        self.seen_urls: Deque[str] = deque(maxlen=100)

    def autoplay_label(self) -> str:
        return self.AUTOPLAY_LABELS.get(self.autoplay_mode, "Desligado")

    def panel_embed(self) -> discord.Embed:
        voice = self.guild.voice_client
        connected = bool(voice and voice.is_connected())

        embed = discord.Embed(
            title="🥭 DJ Mango — Painel",
            color=0xFFB300,
        )

        if self.current:
            embed.description = (
                f"### Tocando agora\n"
                f"[{self.current.title[:180]}]({self.current.webpage_url})"
            )
            if self.current.thumbnail:
                embed.set_thumbnail(url=self.current.thumbnail)
            embed.add_field(
                name="Duração",
                value=format_duration(self.current.duration),
                inline=True,
            )
        elif connected:
            embed.description = "Conectado e aguardando músicas."
        else:
            embed.description = "DJ Mango parado. Use **/play** para começar."

        embed.add_field(
            name="Autoplay",
            value=self.autoplay_label(),
            inline=True,
        )
        embed.add_field(
            name="Volume",
            value=f"{round(self.volume * 100)}%",
            inline=True,
        )

        if voice and voice.channel:
            embed.add_field(
                name="Call",
                value=voice.channel.name[:100],
                inline=True,
            )

        if self.queue:
            upcoming = []
            for index, track in enumerate(list(self.queue)[:4], start=1):
                ready = " ⚡" if track.local_path else ""
                auto = " · Auto" if track.autoplay_generated else ""
                upcoming.append(
                    f"**{index}.** {discord.utils.escape_markdown(track.title[:75])}"
                    f"{ready}{auto}"
                )
            embed.add_field(
                name=f"Próximas ({len(self.queue)})",
                value="\n".join(upcoming),
                inline=False,
            )
        else:
            embed.add_field(
                name="Próximas",
                value="Fila vazia.",
                inline=False,
            )

        embed.set_footer(
            text="⚡ = pré-carregada • arquivos temporários são apagados após tocar"
        )
        return embed

    async def refresh_panel(self) -> None:
        if not self.panel_channel_id or not self.panel_message_id:
            return

        channel = self.bot.get_channel(self.panel_channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return

        try:
            message = await channel.fetch_message(self.panel_message_id)
            await message.edit(
                embed=self.panel_embed(),
                view=MangoControlView(),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            self.panel_channel_id = None
            self.panel_message_id = None

    async def publish_panel(
        self,
        channel: discord.abc.Messageable,
    ) -> Optional[discord.Message]:
        try:
            message = await channel.send(
                embed=self.panel_embed(),
                view=MangoControlView(),
            )
        except discord.HTTPException:
            return None

        self.panel_channel_id = message.channel.id
        self.panel_message_id = message.id
        return message

    async def enqueue(self, track: Track) -> int:
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
            self.idle_task = None

        stale_paths: list[str] = []

        async with self.lock:
            if not track.autoplay_generated and self.queue:
                kept: Deque[Track] = deque()
                for queued in self.queue:
                    if queued.autoplay_generated:
                        if queued.local_path:
                            stale_paths.append(queued.local_path)
                    else:
                        kept.append(queued)
                self.queue = kept

            self.queue.append(track)
            position = len(self.queue)

            voice = self.guild.voice_client
            should_start = (
                not self.closed
                and self.current is None
                and voice is not None
                and voice.is_connected()
                and not voice.is_playing()
                and not voice.is_paused()
            )

        for filepath in stale_paths:
            await asyncio.to_thread(delete_cached_file, filepath)

        await self.refresh_panel()

        if should_start:
            await self.play_next()

        return position

    async def play_next(self) -> None:
        async with self.lock:
            voice = self.guild.voice_client

            if (
                self.closed
                or voice is None
                or not voice.is_connected()
                or self.current is not None
                or voice.is_playing()
                or voice.is_paused()
            ):
                return

            if not self.queue:
                self.schedule_idle_disconnect()
                await self.refresh_panel()
                return

            track = self.queue.popleft()
            self.current = track
            self.seen_urls.append(track.webpage_url)

        try:
            if track.local_path and os.path.exists(track.local_path):
                audio = discord.FFmpegPCMAudio(
                    track.local_path,
                    options=FFMPEG_OPTIONS,
                )
            else:
                stream_url = await asyncio.to_thread(
                    stream_url_sync,
                    track.webpage_url,
                )
                audio = discord.FFmpegPCMAudio(
                    stream_url,
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                )

            source = discord.PCMVolumeTransformer(
                audio,
                volume=self.volume,
            )

            async with self.lock:
                voice = self.guild.voice_client

                if self.closed or voice is None or not voice.is_connected():
                    self.current = None
                    source.cleanup()
                    return

                voice.play(source, after=self.after_callback)

            if self.prefetch_task and not self.prefetch_task.done():
                self.prefetch_task.cancel()

            self.prefetch_task = asyncio.create_task(
                self.prefetch_near_end(track)
            )

            await self.announce_now_playing(track)
            await self.refresh_panel()

        except Exception as exc:
            logger.exception("Falha ao iniciar faixa na guild %s", self.guild.id)

            async with self.lock:
                self.current = None

            if track.local_path:
                await asyncio.to_thread(delete_cached_file, track.local_path)
                track.local_path = None

            await self.send_to_track_channel(
                track,
                f"⚠️ Não consegui tocar **{discord.utils.escape_markdown(track.title)}**: "
                f"{str(exc)[:700]}",
            )

            await self.play_next()

    async def prefetch_near_end(self, seed: Track) -> None:
        if not seed.duration:
            return

        threshold = min(
            max(10, int(seed.duration * 0.30)),
            max(10, int(os.getenv("AUTOPLAY_PREFETCH_SECONDS", "35"))),
        )
        remaining = float(seed.duration)

        try:
            while (
                not self.closed
                and self.current is seed
                and remaining > threshold
            ):
                voice = self.guild.voice_client

                if voice and voice.is_playing():
                    step = min(5.0, remaining - threshold)
                    await asyncio.sleep(step)
                    if voice.is_playing():
                        remaining -= step
                else:
                    await asyncio.sleep(1)

            if not self.closed and self.current is seed:
                await self.prepare_next(seed, after_end=False)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "Falha no pré-carregamento da guild %s",
                self.guild.id,
            )

    async def prepare_next(
        self,
        seed: Track,
        *,
        after_end: bool,
    ) -> None:
        async with self.lock:
            if self.closed:
                return

            target = self.queue[0] if self.queue else None
            mode = self.autoplay_mode

        if target is None and mode != "off":
            seen = set(self.seen_urls)
            async with self.lock:
                seen.update(item.webpage_url for item in self.queue)

            metadata = await asyncio.to_thread(
                recommend_track_sync,
                title=seed.title,
                webpage_url=seed.webpage_url,
                uploader=seed.uploader,
                video_id=seed.video_id,
                mode=mode,
                seen_urls=seen,
            )

            if metadata:
                candidate = Track(
                    title=metadata["title"],
                    webpage_url=metadata["webpage_url"],
                    duration=metadata.get("duration"),
                    uploader=metadata.get("uploader"),
                    requester_name=(
                        "Autoplay • Mesmo estilo"
                        if mode == "similar"
                        else "Autoplay • Mesmo artista"
                    ),
                    text_channel_id=seed.text_channel_id,
                    video_id=metadata.get("video_id"),
                    thumbnail=metadata.get("thumbnail"),
                    autoplay_generated=True,
                )

                async with self.lock:
                    if self.closed or self.autoplay_mode == "off":
                        return

                    if self.queue:
                        target = self.queue[0]
                    elif after_end or self.current is seed:
                        self.queue.append(candidate)
                        target = candidate
                        self.seen_urls.append(candidate.webpage_url)
                    else:
                        return

        if target is not None:
            await self.predownload(target)
            await self.refresh_panel()

    async def predownload(self, track: Track) -> None:
        if track.local_path and os.path.exists(track.local_path):
            return

        async with self.lock:
            if self.current is track:
                return

        try:
            data = await asyncio.to_thread(
                download_audio_sync,
                track.webpage_url,
                self.guild.id,
            )
        except Exception as exc:
            logger.info(
                "Pré-carregamento falhou na guild %s: %s",
                self.guild.id,
                exc,
            )
            return

        async with self.lock:
            still_queued = any(item is track for item in self.queue)
            is_current = self.current is track

            if not still_queued and not is_current:
                keep = False
            else:
                keep = True
                track.local_path = data["path"]
                if data.get("title"):
                    track.title = data["title"]
                if data.get("duration"):
                    track.duration = data["duration"]
                if data.get("uploader"):
                    track.uploader = data["uploader"]
                if data.get("video_id"):
                    track.video_id = data["video_id"]
                if data.get("thumbnail"):
                    track.thumbnail = data["thumbnail"]

        if not keep:
            await asyncio.to_thread(delete_cached_file, data["path"])

    def after_callback(self, error: Optional[Exception]) -> None:
        loop = self.bot.event_loop
        if loop is None:
            return

        future = asyncio.run_coroutine_threadsafe(
            self.on_track_end(error),
            loop,
        )

        def consume(done_future) -> None:
            try:
                done_future.result()
            except Exception:
                logger.exception("Erro ao avançar fila da guild %s", self.guild.id)

        future.add_done_callback(consume)

    async def on_track_end(self, error: Optional[Exception]) -> None:
        if error:
            logger.warning(
                "Faixa encerrada com erro na guild %s: %s",
                self.guild.id,
                error,
            )

        async with self.lock:
            if self.closed:
                return

            ended = self.current
            self.current = None
            prefetch_task = self.prefetch_task
            self.prefetch_task = None

        if ended and ended.local_path:
            await asyncio.to_thread(delete_cached_file, ended.local_path)
            ended.local_path = None

        if prefetch_task and not prefetch_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(prefetch_task),
                    timeout=15,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        async with self.lock:
            queue_empty = not self.queue

        if ended and queue_empty and self.autoplay_mode != "off":
            await self.prepare_next(ended, after_end=True)

        await self.refresh_panel()
        await self.play_next()

    def schedule_idle_disconnect(self) -> None:
        if self.closed:
            return

        if self.idle_task and not self.idle_task.done():
            return

        self.idle_task = asyncio.create_task(self.disconnect_after_idle())

    async def disconnect_after_idle(self) -> None:
        try:
            await asyncio.sleep(idle_timeout())

            async with self.lock:
                if self.closed or self.current is not None or self.queue:
                    return

            await self.destroy(remove_player=True)
        except asyncio.CancelledError:
            return

    async def announce_now_playing(self, track: Track) -> None:
        if self.panel_message_id:
            await self.refresh_panel()
            return

        channel = self.bot.get_channel(track.text_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return

        await self.publish_panel(channel)

    async def send_to_track_channel(self, track: Track, message: str) -> None:
        channel = self.bot.get_channel(track.text_channel_id)

        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(message)
            except discord.HTTPException:
                pass

    async def set_volume(self, percentage: int) -> None:
        self.volume = max(0, min(100, percentage)) / 100

        voice = self.guild.voice_client
        if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = self.volume

        await self.refresh_panel()

    async def destroy(self, *, remove_player: bool = False) -> None:
        if self.closed:
            return

        self.closed = True

        if self.idle_task and self.idle_task is not asyncio.current_task():
            self.idle_task.cancel()

        if self.prefetch_task and self.prefetch_task is not asyncio.current_task():
            self.prefetch_task.cancel()

        async with self.lock:
            cached_paths = [
                track.local_path
                for track in self.queue
                if track.local_path
            ]
            if self.current and self.current.local_path:
                cached_paths.append(self.current.local_path)

            self.queue.clear()
            self.current = None
            voice = self.guild.voice_client

        if voice is not None:
            if voice.is_playing() or voice.is_paused():
                voice.stop()

            try:
                await voice.disconnect(force=True)
            except discord.ClientException:
                pass

        for filepath in cached_paths:
            await asyncio.to_thread(delete_cached_file, filepath)

        await asyncio.to_thread(cleanup_guild_cache, self.guild.id)
        await self.refresh_panel()

        if remove_player:
            self.bot.players.pop(self.guild.id, None)


class MangoControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def send_error(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(f"⚠️ {message}", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"⚠️ {message}",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Pausar / Continuar",
        emoji="⏯️",
        style=discord.ButtonStyle.primary,
        custom_id="mango:toggle_pause",
        row=0,
    )
    async def toggle_pause(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        try:
            player = active_player(interaction)
            assert interaction.guild is not None
            voice = interaction.guild.voice_client
            if voice is None:
                raise UserFacingError("Não há reprodução ativa.")

            if voice.is_paused():
                voice.resume()
            elif voice.is_playing():
                voice.pause()
            else:
                raise UserFacingError("Não há música tocando agora.")

            await interaction.response.edit_message(
                embed=player.panel_embed(),
                view=self,
            )
        except UserFacingError as exc:
            await self.send_error(interaction, str(exc))

    @discord.ui.button(
        label="Pular",
        emoji="⏭️",
        style=discord.ButtonStyle.secondary,
        custom_id="mango:skip",
        row=0,
    )
    async def skip_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        try:
            player = active_player(interaction)
            assert interaction.guild is not None
            voice = interaction.guild.voice_client
            if voice is None or (not voice.is_playing() and not voice.is_paused()):
                raise UserFacingError("Não há música para pular.")

            await interaction.response.defer()
            voice.stop()
            await asyncio.sleep(0.3)
            if interaction.message:
                await interaction.message.edit(
                    embed=player.panel_embed(),
                    view=self,
                )
        except UserFacingError as exc:
            await self.send_error(interaction, str(exc))

    @discord.ui.button(
        label="Parar",
        emoji="⏹️",
        style=discord.ButtonStyle.danger,
        custom_id="mango:stop",
        row=0,
    )
    async def stop_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        try:
            player = active_player(interaction)
            await interaction.response.defer()
            await player.destroy(remove_player=True)
            if interaction.message:
                await interaction.message.edit(
                    embed=player.panel_embed(),
                    view=self,
                )
        except UserFacingError as exc:
            await self.send_error(interaction, str(exc))

    @discord.ui.button(
        label="Embaralhar",
        emoji="🔀",
        style=discord.ButtonStyle.secondary,
        custom_id="mango:shuffle",
        row=0,
    )
    async def shuffle_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        try:
            player = active_player(interaction)

            async with player.lock:
                if len(player.queue) < 2:
                    raise UserFacingError(
                        "Preciso de pelo menos duas músicas na fila."
                    )
                items = list(player.queue)
                random.shuffle(items)
                player.queue = deque(items)

            await interaction.response.edit_message(
                embed=player.panel_embed(),
                view=self,
            )
        except UserFacingError as exc:
            await self.send_error(interaction, str(exc))

    @discord.ui.button(
        label="-10",
        emoji="🔉",
        style=discord.ButtonStyle.secondary,
        custom_id="mango:volume_down",
        row=1,
    )
    async def volume_down(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        try:
            player = active_player(interaction)
            value = max(0, round(player.volume * 100) - 10)
            await player.set_volume(value)
            await interaction.response.edit_message(
                embed=player.panel_embed(),
                view=self,
            )
        except UserFacingError as exc:
            await self.send_error(interaction, str(exc))

    @discord.ui.button(
        label="+10",
        emoji="🔊",
        style=discord.ButtonStyle.secondary,
        custom_id="mango:volume_up",
        row=1,
    )
    async def volume_up(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        try:
            player = active_player(interaction)
            value = min(100, round(player.volume * 100) + 10)
            await player.set_volume(value)
            await interaction.response.edit_message(
                embed=player.panel_embed(),
                view=self,
            )
        except UserFacingError as exc:
            await self.send_error(interaction, str(exc))

    @discord.ui.select(
        placeholder="Autoplay...",
        custom_id="mango:autoplay",
        row=2,
        options=[
            discord.SelectOption(
                label="Autoplay desligado",
                value="off",
                emoji="⏹️",
            ),
            discord.SelectOption(
                label="Mesmo estilo",
                value="similar",
                emoji="🎧",
                description="Continua com músicas relacionadas",
            ),
            discord.SelectOption(
                label="Mesmo artista",
                value="artist",
                emoji="🎤",
                description="Prioriza músicas do mesmo artista",
            ),
        ],
    )
    async def autoplay_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        if interaction.guild is None:
            await self.send_error(interaction, "Esse painel só funciona em servidor.")
            return

        player = bot.player_for(interaction.guild)

        voice = interaction.guild.voice_client
        if voice and voice.is_connected():
            try:
                active_player(interaction)
            except UserFacingError as exc:
                await self.send_error(interaction, str(exc))
                return

        player.autoplay_mode = select.values[0]

        await interaction.response.edit_message(
            embed=player.panel_embed(),
            view=self,
        )

        if (
            player.autoplay_mode != "off"
            and player.current
            and not player.queue
        ):
            if player.prefetch_task and not player.prefetch_task.done():
                return
            player.prefetch_task = asyncio.create_task(
                player.prepare_next(
                    player.current,
                    after_end=False,
                )
            )

class MangoBot(commands.AutoShardedBot):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )

        self.players: dict[int, GuildPlayer] = {}
        self.event_loop: Optional[asyncio.AbstractEventLoop] = None

    def player_for(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)

        if player is None or player.closed:
            player = GuildPlayer(self, guild)
            self.players[guild.id] = player

        return player

    async def setup_hook(self) -> None:
        self.event_loop = asyncio.get_running_loop()
        await asyncio.to_thread(cleanup_stale_cache)
        self.add_view(MangoControlView())

        synced = await self.tree.sync()
        logger.info(
            "%s comandos globais sincronizados. Sharding automático habilitado.",
            len(synced),
        )


bot = MangoBot()


def member_voice_channel(
    interaction: discord.Interaction,
) -> discord.VoiceChannel | discord.StageChannel:
    if interaction.guild is None:
        raise UserFacingError("Esse comando só funciona dentro de um servidor.")

    member = interaction.user

    if not isinstance(member, discord.Member):
        raise UserFacingError("Não consegui identificar seu canal de voz.")

    voice_state = member.voice

    if voice_state is None or voice_state.channel is None:
        raise UserFacingError("Entre em um canal de voz primeiro.")

    channel = voice_state.channel

    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        raise UserFacingError("Não consegui identificar seu canal de voz.")

    return channel


async def ensure_voice(
    interaction: discord.Interaction,
) -> discord.VoiceClient:
    assert interaction.guild is not None

    user_channel = member_voice_channel(interaction)
    voice = interaction.guild.voice_client

    if voice and voice.is_connected():
        if voice.channel and voice.channel.id != user_channel.id:
            raise UserFacingError(
                f"Já estou tocando em **{voice.channel.name}** neste servidor. "
                "A mesma conta do Discord só pode ficar em uma call por servidor de cada vez."
            )

        return voice

    try:
        return await user_channel.connect(self_deaf=True)
    except (discord.ClientException, discord.HTTPException) as exc:
        raise UserFacingError(
            "Não consegui entrar na sua call. Verifique as permissões de "
            "**Ver Canal**, **Conectar** e **Falar**."
        ) from exc


def active_player(interaction: discord.Interaction) -> GuildPlayer:
    assert interaction.guild is not None

    user_channel = member_voice_channel(interaction)
    voice = interaction.guild.voice_client

    if voice is None or not voice.is_connected():
        raise UserFacingError("Não estou conectado a nenhuma call neste servidor.")

    if voice.channel and voice.channel.id != user_channel.id:
        raise UserFacingError(
            f"Estou tocando em **{voice.channel.name}**. Entre nessa call para controlar a música."
        )

    player = bot.players.get(interaction.guild.id)

    if player is None or player.closed:
        raise UserFacingError("Não há uma sessão de música ativa neste servidor.")

    return player


@bot.event
async def on_ready() -> None:
    if not bot.user:
        return

    logger.info(
        "DJ Mango online como %s (%s) | guilds=%s | shards=%s",
        bot.user,
        bot.user.id,
        len(bot.guilds),
        bot.shard_count,
    )

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="/play • DJ Mango 🥭",
        )
    )


@bot.tree.command(name="play", description="Toca uma música do YouTube por nome ou link.")
@app_commands.describe(busca="Nome da música ou link do YouTube")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, busca: str) -> None:
    if not busca.strip():
        await interaction.response.send_message(
            "Informe o nome ou link da música.",
            ephemeral=True,
        )
        return

    try:
        user_channel = member_voice_channel(interaction)

        await interaction.response.defer(thinking=True)

        assert interaction.guild is not None
        assert interaction.channel_id is not None

        await ensure_voice(interaction)

        track = await asyncio.to_thread(
            resolve_track_sync,
            busca,
            interaction.user.display_name,
            interaction.channel_id,
        )

        player = bot.player_for(interaction.guild)
        position = await player.enqueue(track)

        if player.current is track:
            await interaction.followup.send(
                f"▶️ Tocando **{discord.utils.escape_markdown(track.title)}** "
                f"em **{user_channel.name}**."
            )
        else:
            await interaction.followup.send(
                f"➕ **{discord.utils.escape_markdown(track.title)}** adicionada à fila "
                f"(posição {position})."
            )

    except UserFacingError as exc:
        if interaction.response.is_done():
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="pause", description="Pausa a música atual.")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction) -> None:
    try:
        active_player(interaction)
        assert interaction.guild is not None

        voice = interaction.guild.voice_client
        if voice is None or not voice.is_playing():
            raise UserFacingError("Não há nenhuma música tocando agora.")

        voice.pause()
        await interaction.response.send_message("⏸️ Música pausada.")

    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="resume", description="Continua a música pausada.")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction) -> None:
    try:
        active_player(interaction)
        assert interaction.guild is not None

        voice = interaction.guild.voice_client
        if voice is None or not voice.is_paused():
            raise UserFacingError("Não há nenhuma música pausada.")

        voice.resume()
        await interaction.response.send_message("▶️ Reprodução retomada.")

    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="skip", description="Pula a música atual.")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction) -> None:
    try:
        active_player(interaction)
        assert interaction.guild is not None

        voice = interaction.guild.voice_client
        if voice is None or (not voice.is_playing() and not voice.is_paused()):
            raise UserFacingError("Não há nenhuma música para pular.")

        voice.stop()
        await interaction.response.send_message("⏭️ Música pulada.")

    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="queue", description="Mostra a fila de músicas deste servidor.")
@app_commands.guild_only()
async def queue_command(interaction: discord.Interaction) -> None:
    try:
        player = active_player(interaction)

        if player.current is None and not player.queue:
            raise UserFacingError("A fila está vazia.")

        lines: list[str] = []

        if player.current:
            lines.append(
                f"**Tocando:** [{player.current.title[:120]}]"
                f"({player.current.webpage_url})"
            )

        for index, track in enumerate(list(player.queue)[:10], start=1):
            lines.append(
                f"{index}. [{track.title[:100]}]({track.webpage_url}) "
                f"• {format_duration(track.duration)}"
            )

        remaining = max(0, len(player.queue) - 10)
        if remaining:
            lines.append(f"\n… e mais **{remaining}** música(s).")

        embed = discord.Embed(
            title="🎼 Fila do DJ Mango",
            description="\n".join(lines),
            color=0xFFB300,
        )

        await interaction.response.send_message(embed=embed)

    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="nowplaying", description="Mostra a música que está tocando.")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction) -> None:
    try:
        player = active_player(interaction)

        if player.current is None:
            raise UserFacingError("Não há nenhuma música tocando agora.")

        track = player.current

        embed = discord.Embed(
            title="🥭 Tocando agora",
            description=f"[{track.title[:250]}]({track.webpage_url})",
            color=0xFFB300,
        )
        embed.add_field(name="Duração", value=format_duration(track.duration))

        if track.uploader:
            embed.add_field(name="Canal", value=str(track.uploader)[:100])

        await interaction.response.send_message(embed=embed)

    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="volume", description="Altera o volume de 0 a 100.")
@app_commands.describe(porcentagem="Volume entre 0 e 100")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, porcentagem: int) -> None:
    try:
        if porcentagem < 0 or porcentagem > 100:
            raise UserFacingError("O volume precisa estar entre 0 e 100.")

        player = active_player(interaction)
        await player.set_volume(porcentagem)

        await interaction.response.send_message(
            f"🔊 Volume ajustado para **{porcentagem}%**."
        )

    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="stop", description="Limpa a fila e desconecta o DJ Mango.")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction) -> None:
    try:
        player = active_player(interaction)
        await player.destroy(remove_player=True)

        await interaction.response.send_message(
            "⏹️ Fila limpa. DJ Mango saiu da call."
        )

    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="panel", description="Abre o painel clicável do DJ Mango.")
@app_commands.guild_only()
async def panel(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None

    player = bot.player_for(interaction.guild)

    await interaction.response.send_message(
        embed=player.panel_embed(),
        view=MangoControlView(),
    )

    try:
        message = await interaction.original_response()
        player.panel_channel_id = message.channel.id
        player.panel_message_id = message.id
    except discord.HTTPException:
        pass


@bot.tree.command(name="status", description="Mostra o status global do DJ Mango.")
@app_commands.guild_only()
async def status(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🥭 DJ Mango",
        color=0xFFB300,
    )
    embed.add_field(name="Servidores", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Shards", value=str(bot.shard_count or 1), inline=True)
    embed.add_field(
        name="Sessões de música",
        value=str(len(bot.voice_clients)),
        inline=True,
    )
    embed.description = (
        "Um único DJ Mango atende todos os servidores. "
        "Cada servidor mantém sua própria fila e conexão de voz."
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="musichelp", description="Mostra os comandos do DJ Mango.")
@app_commands.guild_only()
async def musichelp(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🥭 DJ Mango — comandos",
        description=(
            "/play <nome ou link> — tocar/adicionar música\n"
            "/pause — pausar\n"
            "/resume — continuar\n"
            "/skip — pular\n"
            "/queue — ver fila\n"
            "/nowplaying — música atual\n"
            "/volume <0-100> — alterar volume\n"
            "/stop — limpar fila e sair\n"
            "/panel — painel com botões e autoplay\n"
            "/status — status global do bot"
        ),
        color=0xFFB300,
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    logger.exception("Erro em slash command: %s", error)

    message = "⚠️ Ocorreu um erro inesperado ao executar esse comando."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()

    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN não foi definido. Configure o token do DJ Mango no Coolify."
        )

    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
