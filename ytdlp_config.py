from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from threading import Lock

logger = logging.getLogger("dj-mango.ytdlp")

_COOKIE_PATH = Path("/tmp/dj-mango-youtube-cookies.txt")
_COOKIE_LOCK = Lock()
_COOKIE_READY = False


def youtube_cookie_file() -> str | None:
    """Resolve yt-dlp cookies from a mounted file or a Coolify env variable."""
    global _COOKIE_READY

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
