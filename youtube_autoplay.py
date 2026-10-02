from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ytdlp_config import music_backend, youtube_cookie_file

logger = logging.getLogger("dj-mango.autoplay")

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
CACHE_ROOT = Path(os.getenv("MUSIC_CACHE_DIR", "/tmp/dj-mango-cache"))


def _ydl_options(*, playlist: bool = False, flat: bool = False) -> dict:
    options = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "source_address": "0.0.0.0",
        "noplaylist": not playlist,
    }

    if flat:
        options["extract_flat"] = "in_playlist"

    cookies = youtube_cookie_file()
    if cookies:
        options["cookiefile"] = cookies

    return options


def _entry_url(entry: dict) -> Optional[str]:
    video_id = entry.get("id")
    url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")

    if isinstance(url, str) and URL_RE.match(url):
        return url

    if video_id and music_backend() == "youtube":
        return f"https://www.youtube.com/watch?v={video_id}"

    return None


def _candidate(entry: dict) -> Optional[dict]:
    url = _entry_url(entry)
    if not url:
        return None

    duration = entry.get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    if duration and duration > 20 * 60:
        return None

    if entry.get("is_live"):
        return None

    return {
        "title": str(entry.get("title") or "Música sugerida"),
        "webpage_url": url,
        "duration": duration,
        "uploader": entry.get("uploader") or entry.get("channel"),
        "video_id": entry.get("id"),
        "thumbnail": entry.get("thumbnail"),
    }


def recommend_track_sync(
    *,
    title: str,
    webpage_url: str,
    uploader: Optional[str],
    video_id: Optional[str],
    mode: str,
    seen_urls: set[str],
) -> Optional[dict]:
    targets: list[tuple[str, bool]] = []

    # YouTube radio URLs only make sense when YouTube is the selected backend.
    # On SoundCloud we use a normal semantic-style search instead.
    if mode == "similar" and video_id and music_backend() == "youtube":
        targets.append(
            (
                f"https://www.youtube.com/watch?v={video_id}"
                f"&list=RD{video_id}&start_radio=1",
                True,
            )
        )

    if mode == "artist" and uploader:
        targets.append((f"ytsearch12:{uploader} official audio", True))
    else:
        seed = " ".join(part for part in [uploader or "", title, "mix"] if part).strip()
        targets.append((f"ytsearch12:{seed}", True))

    for target, flat in targets:
        try:
            options = _ydl_options(playlist=True, flat=flat)
            options["playlistend"] = 15

            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(target, download=False)
        except DownloadError as exc:
            logger.info("Falha ao buscar recomendação em %s: %s", target, exc)
            continue

        if not info:
            continue

        entries = info.get("entries") or [info]

        for entry in entries:
            if not entry:
                continue

            candidate = _candidate(entry)
            if not candidate:
                continue

            url = candidate["webpage_url"]
            if url == webpage_url or url in seen_urls:
                continue

            return candidate

    return None


def download_audio_sync(webpage_url: str, guild_id: int) -> dict:
    guild_dir = CACHE_ROOT / str(guild_id)
    guild_dir.mkdir(parents=True, exist_ok=True)

    token = uuid.uuid4().hex
    template = str(guild_dir / f"{token}.%(ext)s")

    options = _ydl_options()
    options.update(
        {
            "outtmpl": template,
            "overwrites": True,
            "continuedl": False,
        }
    )

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(webpage_url, download=True)
            if not info:
                raise RuntimeError("A fonte de áudio não retornou dados para o download.")

            requested = info.get("requested_downloads") or []
            filepath = requested[0].get("filepath") if requested else None
            if not filepath:
                filepath = ydl.prepare_filename(info)
    except DownloadError as exc:
        raise RuntimeError("Falha ao pré-carregar a próxima música.") from exc

    path = Path(filepath)
    if not path.exists():
        matches = [
            item
            for item in guild_dir.glob(f"{token}.*")
            if item.is_file() and not item.name.endswith(".part")
        ]
        if not matches:
            raise RuntimeError("O arquivo pré-carregado não foi encontrado.")
        path = matches[0]

    duration = info.get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    return {
        "path": str(path),
        "title": str(info.get("title") or ""),
        "duration": duration,
        "uploader": info.get("uploader") or info.get("channel"),
        "video_id": info.get("id"),
        "thumbnail": info.get("thumbnail"),
    }


def delete_cached_file(filepath: Optional[str]) -> None:
    if not filepath:
        return

    try:
        root = CACHE_ROOT.resolve()
        path = Path(filepath).resolve()

        if not path.is_relative_to(root):
            logger.warning("Recusei apagar arquivo fora do cache: %s", path)
            return

        path.unlink(missing_ok=True)

        parent = path.parent
        if parent != root:
            try:
                parent.rmdir()
            except OSError:
                pass
    except OSError as exc:
        logger.warning("Não consegui apagar cache %s: %s", filepath, exc)


def cleanup_guild_cache(guild_id: int) -> None:
    folder = CACHE_ROOT / str(guild_id)
    shutil.rmtree(folder, ignore_errors=True)


def cleanup_stale_cache() -> None:
    shutil.rmtree(CACHE_ROOT, ignore_errors=True)
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
