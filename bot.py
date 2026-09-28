from __future__ import annotations

import asyncio
import logging
import os
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

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("dj-mango")

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"


def _default_volume() -> float:
    try:
        value = int(os.getenv("DEFAULT_VOLUME", "50"))
    except ValueError:
        value = 50
    return max(0, min(value, 100)) / 100


def _idle_timeout() -> int:
    try:
        value = int(os.getenv("IDLE_TIMEOUT", "120"))
    except ValueError:
        value = 120
    return max(30, value)


def _ydl_options() -> dict:
    options = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": False,
        "source_address": "0.0.0.0",
    }
    cookie_file = os.getenv("YTDLP_COOKIES_FILE")
    if cookie_file:
        options["cookiefile"] = cookie_file
    return options


def parse_worker_tokens() -> list[str]:
    raw = os.getenv("DISCORD_WORKER_TOKENS", "")
    if not raw.strip():
        return []

    pieces = re.split(r"[\n,;]+", raw)
    return [piece.strip() for piece in pieces if piece.strip()]


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    duration: Optional[int]
    uploader: Optional[str]
    requester_id: int
    requester_name: str
    text_channel_id: int


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


def _pick_entry(info: dict) -> dict:
    entries = info.get("entries")
    if entries is None:
        return info

    for entry in entries:
        if entry:
            return entry

    raise UserFacingError("Nenhum resultado foi encontrado no YouTube.")


def resolve_track_sync(
    query: str,
    requester_id: int,
    requester_name: str,
    text_channel_id: int,
) -> Track:
    query = query.strip()
    target = query if URL_RE.match(query) else f"ytsearch1:{query}"

    try:
        with YoutubeDL(_ydl_options()) as ydl:
            info = ydl.extract_info(target, download=False)
    except DownloadError as exc:
        logger.warning("Falha do yt-dlp ao resolver %r: %s", query, exc)
        raise UserFacingError(
            "Não consegui encontrar ou acessar essa música no YouTube."
        ) from exc

    if not info:
        raise UserFacingError("Nenhum resultado foi encontrado no YouTube.")

    entry = _pick_entry(info)
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
        requester_id=requester_id,
        requester_name=requester_name,
        text_channel_id=text_channel_id,
    )


def get_stream_url_sync(webpage_url: str) -> str:
    try:
        with YoutubeDL(_ydl_options()) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
    except DownloadError as exc:
        logger.warning("Falha ao obter stream de %s: %s", webpage_url, exc)
        raise UserFacingError(
            "Não consegui abrir o áudio dessa música. Tente novamente."
        ) from exc

    if not info:
        raise UserFacingError("O YouTube não retornou o áudio da música.")

    entry = _pick_entry(info)
    stream_url = entry.get("url")
    if not stream_url:
        raise UserFacingError("O YouTube não retornou um stream de áudio válido.")

    return str(stream_url)


class WorkerClient(discord.Client):
    def __init__(self, worker_number: int) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.worker_number = worker_number

    @property
    def label(self) -> str:
        if self.user:
            return self.user.display_name
        return f"DJ Mango Worker {self.worker_number}"

    async def on_ready(self) -> None:
        if self.user:
            logger.info(
                "Worker %s conectado como %s (%s).",
                self.worker_number,
                self.user,
                self.user.id,
            )


class MusicPool:
    def __init__(self) -> None:
        self.primary: Optional[MangoBot] = None
        self.clients: list[discord.Client] = []
        self.sessions: dict[tuple[int, int], VoiceSession] = {}
        self.lock = asyncio.Lock()
        self.event_loop: Optional[asyncio.AbstractEventLoop] = None

    def configure(
        self,
        primary: "MangoBot",
        clients: list[discord.Client],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.primary = primary
        self.clients = clients
        self.event_loop = loop

    def get_session(self, guild_id: int, voice_channel_id: int) -> Optional["VoiceSession"]:
        session = self.sessions.get((guild_id, voice_channel_id))
        if session and not session.closed:
            return session
        return None

    def _worker_busy_in_guild(self, client: discord.Client, guild_id: int) -> bool:
        for session in self.sessions.values():
            if (
                not session.closed
                and session.guild_id == guild_id
                and session.worker is client
            ):
                return True
        return False

    async def get_or_create_session(
        self,
        guild_id: int,
        voice_channel_id: int,
    ) -> "VoiceSession":
        key = (guild_id, voice_channel_id)

        async with self.lock:
            current = self.sessions.get(key)
            if current and not current.closed:
                return current

            candidates: list[discord.Client] = []
            for client in self.clients:
                if not client.is_ready():
                    continue

                worker_guild = client.get_guild(guild_id)
                if worker_guild is None:
                    continue

                if self._worker_busy_in_guild(client, guild_id):
                    continue

                candidates.append(client)

            if not candidates:
                installed = sum(
                    1
                    for client in self.clients
                    if client.is_ready() and client.get_guild(guild_id) is not None
                )
                raise UserFacingError(
                    "Todas as instâncias do DJ Mango disponíveis neste servidor "
                    f"estão ocupadas ({installed} instalada(s)). "
                    "Adicione outro worker do pool para abrir mais uma call simultânea."
                )

            worker = candidates[0]
            session = VoiceSession(
                pool=self,
                worker=worker,
                guild_id=guild_id,
                voice_channel_id=voice_channel_id,
            )
            self.sessions[key] = session
            return session

    async def release(self, session: "VoiceSession") -> None:
        key = (session.guild_id, session.voice_channel_id)
        async with self.lock:
            if self.sessions.get(key) is session:
                self.sessions.pop(key, None)

    def pool_stats(self, guild_id: int) -> tuple[int, int, int]:
        installed = sum(
            1
            for client in self.clients
            if client.is_ready() and client.get_guild(guild_id) is not None
        )
        active = sum(
            1
            for session in self.sessions.values()
            if not session.closed and session.guild_id == guild_id
        )
        return installed, active, max(0, installed - active)

    def missing_worker_invites(self, guild_id: int) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for client in self.clients:
            if not client.is_ready() or not client.user:
                continue
            if client.get_guild(guild_id) is not None:
                continue

            permissions = discord.Permissions(view_channel=True, connect=True, speak=True)
            invite = (
                "https://discord.com/oauth2/authorize"
                f"?client_id={client.user.id}"
                f"&permissions={permissions.value}"
                "&scope=bot"
            )
            label = getattr(client, "label", client.user.display_name)
            result.append((str(label), invite))
        return result


pool = MusicPool()


class VoiceSession:
    def __init__(
        self,
        pool: MusicPool,
        worker: discord.Client,
        guild_id: int,
        voice_channel_id: int,
    ) -> None:
        self.pool = pool
        self.worker = worker
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.volume = _default_volume()
        self.lock = asyncio.Lock()
        self.closed = False
        self.idle_task: Optional[asyncio.Task] = None

    @property
    def worker_name(self) -> str:
        if self.worker.user:
            return self.worker.user.display_name
        return "DJ Mango"

    def _worker_guild(self) -> discord.Guild:
        guild = self.worker.get_guild(self.guild_id)
        if guild is None:
            raise UserFacingError(
                f"{self.worker_name} ainda não foi adicionado a este servidor."
            )
        return guild

    async def ensure_connected(self) -> discord.VoiceClient:
        guild = self._worker_guild()
        existing = guild.voice_client

        if existing and existing.is_connected():
            if existing.channel and existing.channel.id == self.voice_channel_id:
                return existing
            raise UserFacingError(
                f"{self.worker_name} já está sendo usado em outra call deste servidor."
            )

        channel = guild.get_channel(self.voice_channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise UserFacingError("Não encontrei o canal de voz solicitado.")

        try:
            voice = await channel.connect(self_deaf=True)
        except (discord.ClientException, discord.HTTPException) as exc:
            raise UserFacingError(
                f"{self.worker_name} não conseguiu entrar nessa call. "
                "Verifique as permissões de Ver Canal, Conectar e Falar."
            ) from exc

        return voice

    async def enqueue(self, track: Track) -> int:
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
            self.idle_task = None

        await self.ensure_connected()

        async with self.lock:
            self.queue.append(track)
            position = len(self.queue)
            voice = self._worker_guild().voice_client
            should_start = (
                not self.closed
                and self.current is None
                and voice is not None
                and voice.is_connected()
                and not voice.is_playing()
                and not voice.is_paused()
            )

        if should_start:
            await self.play_next()

        return position

    async def play_next(self) -> None:
        async with self.lock:
            voice = self._worker_guild().voice_client
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
                self._schedule_idle_disconnect()
                return

            track = self.queue.popleft()
            self.current = track

        try:
            stream_url = await asyncio.to_thread(
                get_stream_url_sync,
                track.webpage_url,
            )
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(
                    stream_url,
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                ),
                volume=self.volume,
            )

            async with self.lock:
                voice = self._worker_guild().voice_client
                if self.closed or voice is None or not voice.is_connected():
                    self.current = None
                    source.cleanup()
                    return
                voice.play(source, after=self._after_callback)

            await self._announce_now_playing(track)
        except Exception as exc:
            logger.exception(
                "Falha ao iniciar faixa no servidor %s, call %s.",
                self.guild_id,
                self.voice_channel_id,
            )
            async with self.lock:
                self.current = None

            await self._send_to_track_channel(
                track,
                f"⚠️ Não consegui tocar **{discord.utils.escape_markdown(track.title)}**: "
                f"{str(exc)[:800]}",
            )
            await self.play_next()

    def _after_callback(self, error: Optional[Exception]) -> None:
        loop = self.pool.event_loop
        if loop is None:
            return

        future = asyncio.run_coroutine_threadsafe(
            self._on_track_end(error),
            loop,
        )

        def consume_result(done_future) -> None:
            try:
                done_future.result()
            except Exception:
                logger.exception(
                    "Erro ao avançar fila do servidor %s, call %s.",
                    self.guild_id,
                    self.voice_channel_id,
                )

        future.add_done_callback(consume_result)

    async def _on_track_end(self, error: Optional[Exception]) -> None:
        if error:
            logger.warning("Faixa encerrada com erro: %s", error)

        async with self.lock:
            if self.closed:
                return
            self.current = None

        await self.play_next()

    def _schedule_idle_disconnect(self) -> None:
        if self.closed:
            return
        if self.idle_task and not self.idle_task.done():
            return
        self.idle_task = asyncio.create_task(self._disconnect_after_idle())

    async def _disconnect_after_idle(self) -> None:
        try:
            await asyncio.sleep(_idle_timeout())

            async with self.lock:
                if self.closed or self.current is not None or self.queue:
                    return

            await self.destroy()
        except asyncio.CancelledError:
            return

    async def _announce_now_playing(self, track: Track) -> None:
        if self.pool.primary is None:
            return

        channel = self.pool.primary.get_channel(track.text_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return

        embed = discord.Embed(
            title="🥭 Tocando agora",
            description=f"[{track.title[:250]}]({track.webpage_url})",
            color=0xFFB300,
        )
        embed.add_field(name="Duração", value=format_duration(track.duration))
        if track.uploader:
            embed.add_field(name="Canal", value=str(track.uploader)[:100])
        embed.add_field(name="DJ", value=self.worker_name)
        embed.set_footer(text=f"Pedido por {track.requester_name}")

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            logger.warning("Não foi possível anunciar a faixa.")

    async def _send_to_track_channel(self, track: Track, message: str) -> None:
        if self.pool.primary is None:
            return
        channel = self.pool.primary.get_channel(track.text_channel_id)
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(message)
            except discord.HTTPException:
                logger.warning("Não foi possível enviar mensagem ao canal.")

    async def pause(self) -> None:
        voice = self._worker_guild().voice_client
        if voice is None or not voice.is_playing():
            raise UserFacingError("Não há nenhuma música tocando nesta call.")
        voice.pause()

    async def resume(self) -> None:
        voice = self._worker_guild().voice_client
        if voice is None or not voice.is_paused():
            raise UserFacingError("Não há nenhuma música pausada nesta call.")
        voice.resume()

    async def skip(self) -> None:
        voice = self._worker_guild().voice_client
        if voice is None or (not voice.is_playing() and not voice.is_paused()):
            raise UserFacingError("Não há nenhuma música para pular nesta call.")
        voice.stop()

    async def set_volume(self, percentage: int) -> None:
        self.volume = percentage / 100
        voice = self._worker_guild().voice_client
        if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = self.volume

    async def destroy(self) -> None:
        if self.closed:
            return

        self.closed = True
        if self.idle_task and self.idle_task is not asyncio.current_task():
            self.idle_task.cancel()

        async with self.lock:
            self.queue.clear()
            self.current = None
            voice = self._worker_guild().voice_client

        if voice is not None:
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            try:
                await voice.disconnect(force=True)
            except discord.ClientException:
                pass

        await self.pool.release(self)


class MangoBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self) -> None:
        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            try:
                guild = discord.Object(id=int(guild_id))
            except ValueError:
                raise RuntimeError("DISCORD_GUILD_ID precisa ser um número inteiro.")

            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                "%s slash commands sincronizados no servidor de teste %s.",
                len(synced),
                guild_id,
            )
        else:
            synced = await self.tree.sync()
            logger.info("%s slash commands globais sincronizados.", len(synced))


bot = MangoBot()


def member_voice_channel(interaction: discord.Interaction) -> discord.VoiceChannel | discord.StageChannel:
    if interaction.guild is None:
        raise UserFacingError("Esse comando só pode ser usado dentro de um servidor.")

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


def session_for_interaction(interaction: discord.Interaction) -> VoiceSession:
    channel = member_voice_channel(interaction)
    assert interaction.guild is not None
    session = pool.get_session(interaction.guild.id, channel.id)
    if session is None:
        raise UserFacingError(
            "Não há uma sessão do DJ Mango ativa na sua call. Use /play primeiro."
        )
    return session


def now_playing_embed(session: VoiceSession) -> discord.Embed:
    track = session.current
    assert track is not None

    embed = discord.Embed(
        title="🎶 Música atual",
        description=f"[{track.title[:250]}]({track.webpage_url})",
        color=0xFFB300,
    )
    embed.add_field(name="Duração", value=format_duration(track.duration))
    if track.uploader:
        embed.add_field(name="Canal", value=str(track.uploader)[:100])
    embed.add_field(name="DJ", value=session.worker_name)
    return embed


@bot.event
async def on_ready() -> None:
    if bot.user:
        logger.info("DJ Mango principal conectado como %s (%s).", bot.user, bot.user.id)
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="/play • DJ Mango 🥭",
            )
        )


@bot.tree.command(name="play", description="Toca YouTube por nome ou link na sua call.")
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
        voice_channel = member_voice_channel(interaction)
        await interaction.response.defer(thinking=True)

        assert interaction.guild is not None
        assert interaction.channel_id is not None

        track = await asyncio.to_thread(
            resolve_track_sync,
            busca,
            interaction.user.id,
            interaction.user.display_name,
            interaction.channel_id,
        )

        session = await pool.get_or_create_session(
            interaction.guild.id,
            voice_channel.id,
        )
        position = await session.enqueue(track)

        if session.current is track:
            await interaction.followup.send(
                f"▶️ **{session.worker_name}** entrou em **{voice_channel.name}** e está "
                f"preparando **{discord.utils.escape_markdown(track.title)}**."
            )
        else:
            await interaction.followup.send(
                f"➕ **{discord.utils.escape_markdown(track.title)}** adicionada à fila "
                f"de **{voice_channel.name}** (posição {position}) • DJ: **{session.worker_name}**."
            )
    except UserFacingError as exc:
        if interaction.response.is_done():
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="pause", description="Pausa a música da sua call.")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction) -> None:
    try:
        session = session_for_interaction(interaction)
        await session.pause()
        await interaction.response.send_message("⏸️ Música pausada.")
    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="resume", description="Continua a música pausada da sua call.")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction) -> None:
    try:
        session = session_for_interaction(interaction)
        await session.resume()
        await interaction.response.send_message("▶️ Reprodução retomada.")
    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="skip", description="Pula a música atual da sua call.")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction) -> None:
    try:
        session = session_for_interaction(interaction)
        await session.skip()
        await interaction.response.send_message("⏭️ Música pulada.")
    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="queue", description="Mostra a fila da sua call.")
@app_commands.guild_only()
async def queue_command(interaction: discord.Interaction) -> None:
    try:
        session = session_for_interaction(interaction)
    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
        return

    if session.current is None and not session.queue:
        await interaction.response.send_message("📭 A fila desta call está vazia.")
        return

    lines: list[str] = []
    if session.current:
        lines.append(
            f"**Tocando:** [{session.current.title[:120]}]({session.current.webpage_url})"
        )

    for index, track in enumerate(list(session.queue)[:10], start=1):
        lines.append(
            f"{index}. [{track.title[:100]}]({track.webpage_url}) "
            f"• {format_duration(track.duration)}"
        )

    remaining = max(0, len(session.queue) - 10)
    if remaining:
        lines.append(f"\n… e mais **{remaining}** música(s).")

    embed = discord.Embed(
        title=f"🎼 Fila — {session.worker_name}",
        description="\n".join(lines),
        color=0xFFB300,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Mostra a música tocando na sua call.")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction) -> None:
    try:
        session = session_for_interaction(interaction)
        if session.current is None:
            raise UserFacingError("Não há nenhuma música tocando nesta call.")
        await interaction.response.send_message(embed=now_playing_embed(session))
    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="volume", description="Altera o volume da sua call de 0 a 100.")
@app_commands.describe(porcentagem="Volume entre 0 e 100")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, porcentagem: int) -> None:
    try:
        if porcentagem < 0 or porcentagem > 100:
            raise UserFacingError("O volume precisa estar entre 0 e 100.")
        session = session_for_interaction(interaction)
        await session.set_volume(porcentagem)
        await interaction.response.send_message(
            f"🔊 Volume desta call ajustado para **{porcentagem}%**."
        )
    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="stop", description="Limpa a fila e tira o DJ da sua call.")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction) -> None:
    try:
        session = session_for_interaction(interaction)
        worker_name = session.worker_name
        await session.destroy()
        await interaction.response.send_message(
            f"⏹️ Fila limpa. **{worker_name}** saiu da sua call."
        )
    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="pool", description="Mostra a capacidade de DJs simultâneos neste servidor.")
@app_commands.guild_only()
async def pool_command(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None

    installed, active, free = pool.pool_stats(interaction.guild.id)
    total = len(pool.clients)

    embed = discord.Embed(
        title="🥭 DJ Mango — Pool de música",
        color=0xFFB300,
    )
    embed.add_field(name="Bots configurados", value=str(total), inline=True)
    embed.add_field(name="Bots neste servidor", value=str(installed), inline=True)
    embed.add_field(name="Calls ativas", value=str(active), inline=True)
    embed.add_field(name="Livres neste servidor", value=str(free), inline=True)
    embed.description = (
        "Cada bot do pool pode ocupar **1 call por servidor**. "
        "O mesmo bot pode tocar em vários servidores diferentes ao mesmo tempo."
    )

    missing = pool.missing_worker_invites(interaction.guild.id)
    if missing:
        links = "\n".join(
            f"• [{discord.utils.escape_markdown(name)}]({url})"
            for name, url in missing[:8]
        )
        embed.add_field(
            name="Workers que ainda podem ser adicionados",
            value=links,
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="musichelp", description="Mostra os comandos do DJ Mango.")
@app_commands.guild_only()
async def musichelp(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🥭 DJ Mango — comandos",
        description=(
            "/play <nome ou link> — tocar/adicionar música na sua call\n"
            "/pause — pausar sua call\n"
            "/resume — continuar\n"
            "/skip — pular\n"
            "/queue — ver fila da sua call\n"
            "/nowplaying — música atual\n"
            "/volume <0-100> — alterar volume\n"
            "/stop — limpar fila e sair da sua call\n"
            "/pool — ver quantas calls simultâneas estão disponíveis"
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


async def run_all_clients() -> None:
    primary_token = os.getenv("DISCORD_TOKEN", "").strip()
    if not primary_token:
        raise RuntimeError(
            "DISCORD_TOKEN não foi definido. Configure o token do DJ Mango principal."
        )

    worker_tokens = parse_worker_tokens()
    workers = [WorkerClient(index + 1) for index in range(len(worker_tokens))]
    clients: list[discord.Client] = [bot, *workers]

    pool.configure(bot, clients, asyncio.get_running_loop())

    tasks = [asyncio.create_task(bot.start(primary_token))]
    tasks.extend(
        asyncio.create_task(worker.start(token))
        for worker, token in zip(workers, worker_tokens)
    )

    logger.info(
        "Iniciando DJ Mango com %s bot(s): 1 principal + %s worker(s).",
        len(clients),
        len(workers),
    )

    try:
        await asyncio.gather(*tasks)
    finally:
        await asyncio.gather(
            *(client.close() for client in clients if not client.is_closed()),
            return_exceptions=True,
        )


def main() -> None:
    asyncio.run(run_all_clients())


if __name__ == "__main__":
    main()
