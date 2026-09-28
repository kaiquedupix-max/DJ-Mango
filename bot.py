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


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    duration: Optional[int]
    uploader: Optional[str]
    requester_id: int
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


def resolve_track_sync(query: str, requester_id: int, text_channel_id: int) -> Track:
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


class GuildPlayer:
    def __init__(self, bot: "MusicBot", guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.volume = _default_volume()
        self.lock = asyncio.Lock()
        self.closed = False

    async def enqueue(self, track: Track) -> int:
        async with self.lock:
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
                voice = self.guild.voice_client
                if self.closed or voice is None or not voice.is_connected():
                    self.current = None
                    source.cleanup()
                    return
                voice.play(source, after=self._after_callback)

            await self._announce_now_playing(track)
        except Exception as exc:
            logger.exception("Falha ao iniciar faixa em %s", self.guild.id)
            async with self.lock:
                self.current = None

            await self._send_to_track_channel(
                track,
                f"⚠️ Não consegui tocar **{discord.utils.escape_markdown(track.title)}**: {exc}",
            )
            await self.play_next()

    def _after_callback(self, error: Optional[Exception]) -> None:
        future = asyncio.run_coroutine_threadsafe(
            self._on_track_end(error),
            self.bot.loop,
        )

        def consume_result(done_future) -> None:
            try:
                done_future.result()
            except Exception:
                logger.exception("Erro ao avançar a fila do servidor %s", self.guild.id)

        future.add_done_callback(consume_result)

    async def _on_track_end(self, error: Optional[Exception]) -> None:
        if error:
            logger.warning("Faixa encerrada com erro: %s", error)

        async with self.lock:
            if self.closed:
                return
            self.current = None

        await self.play_next()

    async def _announce_now_playing(self, track: Track) -> None:
        channel = self.bot.get_channel(track.text_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return

        embed = discord.Embed(
            title="🎵 Tocando agora",
            description=f"[{track.title[:250]}]({track.webpage_url})",
        )
        embed.add_field(name="Duração", value=format_duration(track.duration))
        if track.uploader:
            embed.add_field(name="Canal", value=str(track.uploader)[:100])
        embed.set_footer(text=f"Pedido por usuário {track.requester_id}")

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            logger.warning("Não foi possível anunciar a faixa.")

    async def _send_to_track_channel(self, track: Track, message: str) -> None:
        channel = self.bot.get_channel(track.text_channel_id)
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(message)
            except discord.HTTPException:
                logger.warning("Não foi possível enviar mensagem ao canal.")

    async def destroy(self) -> None:
        async with self.lock:
            self.closed = True
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


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.players: dict[int, GuildPlayer] = {}

    def player_for(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if player is None or player.closed:
            player = GuildPlayer(self, guild)
            self.players[guild.id] = player
        return player

    async def close_player(self, guild: discord.Guild) -> None:
        player = self.players.pop(guild.id, None)
        if player:
            await player.destroy()
        elif guild.voice_client:
            await guild.voice_client.disconnect(force=True)

    async def setup_hook(self) -> None:
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            try:
                guild = discord.Object(id=int(guild_id))
            except ValueError:
                raise RuntimeError("DISCORD_GUILD_ID precisa ser um número inteiro.")

            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                "%s slash commands sincronizados no servidor %s.",
                len(synced),
                guild_id,
            )
        else:
            synced = await self.tree.sync()
            logger.info("%s slash commands globais sincronizados.", len(synced))


bot = MusicBot()


async def require_voice_channel(
    interaction: discord.Interaction,
    *,
    connect: bool = False,
) -> discord.VoiceChannel | discord.StageChannel:
    if interaction.guild is None:
        raise UserFacingError("Esse comando só pode ser usado dentro de um servidor.")

    member = interaction.user
    if not isinstance(member, discord.Member):
        raise UserFacingError("Não consegui identificar seu canal de voz.")

    voice_state = member.voice
    if voice_state is None or voice_state.channel is None:
        raise UserFacingError("Entre em um canal de voz primeiro.")

    user_channel = voice_state.channel
    voice_client = interaction.guild.voice_client

    if voice_client and voice_client.is_connected():
        if voice_client.channel and voice_client.channel.id != user_channel.id:
            raise UserFacingError("Entre no mesmo canal de voz em que o DJ Mango está.")
    elif connect:
        try:
            await user_channel.connect()
        except discord.ClientException as exc:
            raise UserFacingError("Não consegui entrar no canal de voz.") from exc

    return user_channel


def now_playing_embed(track: Track) -> discord.Embed:
    embed = discord.Embed(
        title="🎶 Música atual",
        description=f"[{track.title[:250]}]({track.webpage_url})",
    )
    embed.add_field(name="Duração", value=format_duration(track.duration))
    if track.uploader:
        embed.add_field(name="Canal", value=str(track.uploader)[:100])
    return embed


@bot.event
async def on_ready() -> None:
    if bot.user:
        logger.info("DJ Mango conectado como %s (%s)", bot.user, bot.user.id)
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="/play",
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
        await require_voice_channel(interaction)
        await interaction.response.defer(thinking=True)

        assert interaction.guild is not None
        assert interaction.channel_id is not None

        track = await asyncio.to_thread(
            resolve_track_sync,
            busca,
            interaction.user.id,
            interaction.channel_id,
        )
        await require_voice_channel(interaction, connect=True)

        player = bot.player_for(interaction.guild)
        position = await player.enqueue(track)

        if player.current is track:
            await interaction.followup.send(
                f"▶️ Preparando **{discord.utils.escape_markdown(track.title)}**."
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
        await require_voice_channel(interaction)
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
        await require_voice_channel(interaction)
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
        await require_voice_channel(interaction)
        assert interaction.guild is not None
        voice = interaction.guild.voice_client
        if voice is None or (not voice.is_playing() and not voice.is_paused()):
            raise UserFacingError("Não há nenhuma música para pular.")

        voice.stop()
        await interaction.response.send_message("⏭️ Música pulada.")
    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="queue", description="Mostra a fila de músicas.")
@app_commands.guild_only()
async def queue_command(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    player = bot.players.get(interaction.guild.id)

    if player is None or (player.current is None and not player.queue):
        await interaction.response.send_message("📭 A fila está vazia.")
        return

    lines: list[str] = []
    if player.current:
        lines.append(
            f"**Tocando:** [{player.current.title[:120]}]({player.current.webpage_url})"
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
        title=f"🎼 Fila do DJ Mango ({len(player.queue)} aguardando)",
        description="\n".join(lines),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Mostra a música que está tocando.")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    player = bot.players.get(interaction.guild.id)

    if player is None or player.current is None:
        await interaction.response.send_message("📭 Não há nenhuma música tocando.")
        return

    await interaction.response.send_message(embed=now_playing_embed(player.current))


@bot.tree.command(name="volume", description="Altera o volume de 0 a 100.")
@app_commands.describe(porcentagem="Volume entre 0 e 100")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, porcentagem: int) -> None:
    try:
        await require_voice_channel(interaction)
        if porcentagem < 0 or porcentagem > 100:
            raise UserFacingError("O volume precisa estar entre 0 e 100.")

        assert interaction.guild is not None
        player = bot.player_for(interaction.guild)
        player.volume = porcentagem / 100

        voice = interaction.guild.voice_client
        if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = player.volume

        await interaction.response.send_message(
            f"🔊 Volume ajustado para **{porcentagem}%**."
        )
    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="stop", description="Limpa a fila e desconecta o bot.")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction) -> None:
    try:
        await require_voice_channel(interaction)
        assert interaction.guild is not None
        await bot.close_player(interaction.guild)
        await interaction.response.send_message(
            "⏹️ Fila limpa. DJ Mango saiu do canal de voz."
        )
    except UserFacingError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


@bot.tree.command(name="musichelp", description="Mostra os comandos de música.")
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
            "/stop — limpar fila e sair"
        ),
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
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN não foi definido. Copie .env.example para .env e configure o token."
        )

    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
