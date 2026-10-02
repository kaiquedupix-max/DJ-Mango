from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from threading import Lock

from yt_dlp import YoutubeDL

logger = logging.getLogger("dj-mango.ytdlp")

_COOKIE_PATH = Path("/tmp/dj-mango-youtube-cookies.txt")
_COOKIE_LOCK = Lock()
_COOKIE_READY = False
_PATCH_LOCK = Lock()
_PATCHED = False

_YT_SEARCH_RE = re.compile(r"^ytsearch(?P<count>\d*):(?P<query>.+)$", re.IGNORECASE)


def music_backend() -> str:
    value = os.getenv("MUSIC_BACKEND", "soundcloud").strip().lower()
    if value in {"youtube", "yt"}:
        return "youtube"
    return "soundcloud"


def _rewrite_search_target(target):
    """Route textual YouTube searches through SoundCloud when configured.

    The rest of the bot can keep using its existing yt-dlp calls. Direct URLs are
    intentionally left untouched, while name/artist searches avoid YouTube's
    datacenter login challenge.
    """
    if music_backend() != "soundcloud" or not isinstance(target, str):
        return target

    match = _YT_SEARCH_RE.match(target)
    if not match:
        return target

    count = match.group("count") or "1"
    query = match.group("query").strip()
    rewritten = f"scsearch{count}:{query}"
    logger.debug("Busca roteada para SoundCloud: %r -> %r", target, rewritten)
    return rewritten


def _install_backend_compat() -> None:
    """Install a tiny compatibility shim for the existing bot search calls."""
    global _PATCHED

    with _PATCH_LOCK:
        if _PATCHED:
            return

        original_extract_info = YoutubeDL.extract_info

        def extract_info_with_backend(self, url, *args, **kwargs):
            return original_extract_info(
                self,
                _rewrite_search_target(url),
                *args,
                **kwargs,
            )

        YoutubeDL.extract_info = extract_info_with_backend
        _PATCHED = True
        logger.info("Backend de música ativo: %s.", music_backend())


def youtube_cookie_file() -> str | None:
    """Resolve yt-dlp cookies from a mounted file or a Coolify env variable."""
    global _COOKIE_READY

    # SoundCloud does not need the YouTube session. Returning None also prevents
    # stale YouTube cookies from being attached to normal music searches.
    if music_backend() == "soundcloud":
        return None

    configured = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return str(path)
        logger.warning("YTDLP_COOKIES_FILE aponta para um arquivo inexistente: %s", path)

    encoded = os.getenv("YTDLP_COOKIES_B64", "").strip()
    if not encoded:
        return None

    with _COOKIE_LOCK:
        if not _COOKIE_READY:
            try:
                payload = base64.b64decode(encoded, validate=True)
                text = payload.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                logger.error("YTDLP_COOKIES_B64 inválido: %s", exc)
                return None

            if "youtube.com" not in text and ".youtube.com" not in text:
                logger.warning(
                    "YTDLP_COOKIES_B64 foi carregado, mas não parece conter cookies do YouTube."
                )

            _COOKIE_PATH.write_text(text, encoding="utf-8")
            try:
                _COOKIE_PATH.chmod(0o600)
            except OSError:
                pass
            _COOKIE_READY = True
            logger.info("Cookies do YouTube carregados pela variável YTDLP_COOKIES_B64.")

    return str(_COOKIE_PATH)


_install_backend_compat()
