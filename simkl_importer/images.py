"""Poster thumbnails rendered as colour blocks in the terminal.

Simkl serves images through https://wsrv.nl/, which can resize server-side, so
we ask for a thumbnail barely bigger than the terminal cells we are going to
paint. Their docs require images to be cached by URL forever, so downloads land
in ~/.simkl-importer/posters/ and are only fetched once.

Rendering uses the upper-half-block trick: each character cell paints two
pixels (foreground = top, background = bottom), which keeps the poster's aspect
ratio roughly correct and needs nothing beyond 24-bit colour. Windows Terminal,
iTerm2, GNOME Terminal and friends all handle it.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import List, Optional

# Overridable only so the test-suite can point at a local stub; leave alone.
IMAGE_HOST = os.environ.get("SIMKL_IMAGE_BASE", "https://wsrv.nl/?url=https://simkl.in")
UPPER_HALF_BLOCK = "▀"
RESET = "\033[0m"

DEFAULT_WIDTH = 22  # character cells

try:  # Pillow is optional - without it we just print the URL
    from PIL import Image

    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the install
    Image = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False


def poster_url(poster: str, size: str = "_c", extension: str = "jpg", width: Optional[int] = None) -> str:
    """Build a poster URL. `poster` is the raw field from the API ("54/abc...")."""
    if not poster:
        return ""
    url = f"{IMAGE_HOST}/posters/{poster}{size}.{extension}"
    if width:
        # wsrv resizes for us, so we download a few KB instead of a few hundred
        url += f"&w={width}&output=jpg"
    return url


def supports_colour() -> bool:
    """Best-effort check for a terminal that can show 24-bit colour."""
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.name == "nt":
        # Windows Terminal / PowerShell 7 set WT_SESSION; older consoles do not
        # handle ANSI reliably, and colorama is not a dependency here.
        return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"))
    return os.environ.get("TERM", "dumb") != "dumb"


class PosterRenderer:
    """Downloads, caches and paints poster thumbnails."""

    def __init__(self, session, cache_dir: Path, width: int = DEFAULT_WIDTH, enabled: bool = True):
        self.session = session
        self.cache_dir = Path(cache_dir)
        self.width = max(8, min(60, width))
        self.enabled = enabled and PILLOW_AVAILABLE and supports_colour()
        self.reason = ""
        if enabled and not PILLOW_AVAILABLE:
            self.reason = "install Pillow (pip install pillow) to see poster thumbnails"
        elif enabled and not supports_colour():
            self.reason = "this terminal does not report 24-bit colour support"
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ cache

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{digest}.jpg"

    def fetch(self, url: str) -> Optional[bytes]:
        """Image bytes for a URL, cached on disk forever (per Simkl's rules)."""
        path = self._cache_path(url)
        if path.exists():
            try:
                return path.read_bytes()
            except OSError:
                pass
        try:
            response = self.session.get(url, timeout=15)
            if response.status_code != 200 or not response.content:
                return None
        except Exception:
            return None
        try:
            path.write_bytes(response.content)
        except OSError:
            pass  # cache is a nicety, not a requirement
        return response.content

    # ----------------------------------------------------------------- render

    def render_lines(self, poster: str) -> List[str]:
        """ANSI lines for a poster, or [] when we cannot draw it."""
        if not self.enabled or not poster:
            return []

        # ask wsrv for roughly the pixel grid we need: `width` px across, and
        # two vertical pixels per character row
        url = poster_url(poster, size="_c", width=self.width * 2)
        payload = self.fetch(url)
        if not payload:
            return []

        try:
            import io

            with Image.open(io.BytesIO(payload)) as source:
                image = source.convert("RGB")
                ratio = image.height / image.width if image.width else 1.5
                rows = max(2, int(round(self.width * ratio)))
                if rows % 2:
                    rows += 1  # two pixel rows per character cell
                image = image.resize((self.width, rows), Image.LANCZOS)
                pixels = image.load()
                lines = []
                for y in range(0, rows, 2):
                    chunks = []
                    for x in range(self.width):
                        tr, tg, tb = pixels[x, y]
                        br, bg, bb = pixels[x, y + 1]
                        chunks.append(
                            f"\033[38;2;{tr};{tg};{tb}m\033[48;2;{br};{bg};{bb}m{UPPER_HALF_BLOCK}"
                        )
                    lines.append("".join(chunks) + RESET)
                return lines
        except Exception:
            return []

    def render_beside(self, poster: str, text_lines: List[str], gap: int = 2) -> str:
        """Poster on the left, details on the right, padded to the same height."""
        art = self.render_lines(poster)
        if not art:
            return "\n".join(text_lines)

        height = max(len(art), len(text_lines))
        art += [" " * self.width] * (height - len(art))
        text_lines = list(text_lines) + [""] * (height - len(text_lines))
        pad = " " * gap
        return "\n".join(f"{art[i]}{pad}{text_lines[i]}" for i in range(height))
