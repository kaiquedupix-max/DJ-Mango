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
    def __init__(self, bot: "MangoBot", guild: discord.Guild) -> None:
        self.bot = bot
        self.guild = guild
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.volume = default_volume()
        self.lock = asyncio.Lock()
        self.closed = False
        self.idle_task: Optional[asyncio.Task] = None

    async def enqueue(self, track: Track) -> int:
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
            self.idle_task = None

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
                self.schedule_idle_disconnect()
                return

            track = self.queue.popleft()
            self.current = track

        try:
            stream_url = await asyncio.to_thread(
                stream_url_sync,
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

                voice.play(source, after=self.after_callback)

            await self.announce_now_playing(track)

        except Exception as exc:
            logger.exception("Falha ao iniciar faixa na guild %s", self.guild.id)

            async with self.lock:
                self.current = None

            await self.send_to_track_channel(
                track,
                f"⚠️ Não consegui tocar **{discord.utils.escape_markdown(track.title)}**: "
                f"{str(exc)[:700]}",
            )

            await self.play_next()

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
            logger.warning("Faixa encerrada com erro na guild %s: %s", self.guild.id, error)

        async with self.lock:
            if self.closed:
                return
            self.current = None

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
        channel = self.bot.get_channel(track.text_channel_id)

        if not isinstance(channel, discord.abc.Messageable):
            return

        embed = discord.Embed(
            title="🥭 Tocando agora",
            description=f"[{track.title[:250]}]({track.webpage_url})",
            color=0xFFB300,
        )
        embed.add_field(name="Duração", value=format_duration(track.duration), inline=True)

        if track.uploader:
            embed.add_field(
                name="Canal",
                value=str(track.uploader)[:100],
                inline=True,
            )

        embed.add_field(
            name="Pedido por",
            value=track.requester_name[:100],
            inline=True,
        )

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            logger.warning("Não foi possível anunciar a faixa na guild %s", self.guild.id)

    async def send_to_track_channel(self, track: Track, message: str) -> None:
        channel = self.bot.get_channel(track.text_channel_id)

        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(message)
            except discord.HTTPException:
                pass

    async def set_volume(self, percentage: int) -> None:
        self.volume = percentage / 100

        voice = self.guild.voice_client
        if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = self.volume

    async def destroy(self, *, remove_player: bool = False) -> None:
        if self.closed:
            return

        self.closed = True

        if self.idle_task and self.idle_task is not asyncio.current_task():
            self.idle_task.cancel()

        async with self.lock:
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

        if remove_player:
            self.bot.players.pop(self.guild.id, None)


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
