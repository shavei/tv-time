"""Credential prompting/storage and the local daily-request budget.

Nothing here ever gets written into the repository: the default home is
``~/.simkl-importer`` and the files are created 0600.
"""

from __future__ import annotations

import getpass
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

APP_NAME = "tv-time-simkl-importer"
APP_VERSION = "1.0"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"

# Overridable only so the test-suite can point at a local stub server; leave
# these alone in normal use.
API_BASE = os.environ.get("SIMKL_API_BASE", "https://api.simkl.com")
WEB_BASE = os.environ.get("SIMKL_WEB_BASE", "https://simkl.com")
OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"

# The developer console shows this as the free-tier app quota. It is enforced
# server-side; we track it locally so a big import fails loudly and early
# rather than halfway through with opaque errors.
DEFAULT_DAILY_LIMIT = 1000


def default_home() -> Path:
    return Path(os.environ.get("SIMKL_IMPORTER_HOME", Path.home() / ".simkl-importer"))


@dataclass
class Credentials:
    client_id: str
    client_secret: str = ""
    access_token: str = ""
    redirect_uri: str = OOB_REDIRECT_URI

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "access_token": self.access_token,
            "redirect_uri": self.redirect_uri,
        }


class Store:
    """Small JSON-file store for credentials, caches and the request budget."""

    def __init__(self, home: Optional[Path] = None):
        self.home = Path(home) if home else default_home()
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            self.home.chmod(0o700)
        except OSError:
            pass  # non-POSIX filesystems; not worth failing over
        self.credentials_path = self.home / "credentials.json"
        self.usage_path = self.home / "usage.json"
        self.cache_path = self.home / "match-cache.json"
        self.queue_path = self.home / "queue.json"
        self.library_path = self.home / "library.json"
        self.rejected_path = self.home / "rejected.json"
        self.genre_cache_path = self.home / "genre-cache.json"
        self.detail_cache_path = self.home / "detail-cache.json"
        self.accepted_path = self.home / "accepted.json"
        self.poster_dir = self.home / "posters"

    # ------------------------------------------------------------- json utils

    @staticmethod
    def _read_json(path: Path, fallback: Any) -> Any:
        if not path.exists():
            return fallback
        try:
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return fallback

    @staticmethod
    def _write_json(path: Path, payload: Any, private: bool = False) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        if private:
            try:
                path.chmod(0o600)
            except OSError:
                pass

    # ----------------------------------------------------------- credentials

    def load_credentials(self) -> Optional[Credentials]:
        raw = self._read_json(self.credentials_path, None)
        env_id = os.environ.get("SIMKL_CLIENT_ID")
        env_secret = os.environ.get("SIMKL_CLIENT_SECRET")
        env_token = os.environ.get("SIMKL_ACCESS_TOKEN")
        if raw is None and not env_id:
            return None
        raw = raw or {}
        client_id = env_id or raw.get("client_id", "")
        if not client_id:
            return None
        return Credentials(
            client_id=client_id,
            client_secret=env_secret or raw.get("client_secret", ""),
            access_token=env_token or raw.get("access_token", ""),
            redirect_uri=raw.get("redirect_uri", OOB_REDIRECT_URI),
        )

    def save_credentials(self, credentials: Credentials) -> None:
        self._write_json(self.credentials_path, credentials.to_dict(), private=True)

    def prompt_credentials(self, existing: Optional[Credentials] = None) -> Credentials:
        """Ask for client_id/secret, keeping any values we already have."""
        print("\nSimkl app credentials (https://simkl.com/settings/developer/)")
        if existing and existing.client_id:
            masked = existing.client_id[:6] + "..." + existing.client_id[-4:]
            print(f"  Stored Client ID: {masked}")
        client_id = input("  Client ID [keep stored]: ").strip()
        if not client_id and existing:
            client_id = existing.client_id
        while not client_id:
            client_id = input("  Client ID (required): ").strip()

        prompt = "  Client Secret (hidden) [keep stored]: " if existing else "  Client Secret (hidden): "
        client_secret = getpass.getpass(prompt).strip()
        if not client_secret and existing:
            client_secret = existing.client_secret

        credentials = Credentials(
            client_id=client_id,
            client_secret=client_secret,
            access_token=existing.access_token if existing else "",
        )
        self.save_credentials(credentials)
        print(f"  Saved to {self.credentials_path} (mode 0600)")
        return credentials

    # ---------------------------------------------------------- daily budget

    def _usage(self) -> Dict[str, Any]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        usage = self._read_json(self.usage_path, {})
        if usage.get("date") != today:
            usage = {"date": today, "requests": 0}
        return usage

    def requests_today(self) -> int:
        return int(self._usage().get("requests", 0))

    def record_requests(self, count: int = 1) -> int:
        usage = self._usage()
        usage["requests"] = int(usage.get("requests", 0)) + count
        self._write_json(self.usage_path, usage)
        return usage["requests"]

    # ---------------------------------------------------------------- caches

    def load_cache(self) -> Dict[str, Any]:
        return self._read_json(self.cache_path, {})

    def save_cache(self, cache: Dict[str, Any]) -> None:
        self._write_json(self.cache_path, cache)

    def load_queue(self) -> list:
        return self._read_json(self.queue_path, [])

    def save_queue(self, queue: list) -> None:
        self._write_json(self.queue_path, queue)

    def clear_queue(self) -> None:
        if self.queue_path.exists():
            self.queue_path.unlink()

    def load_rejected(self) -> Dict[str, Any]:
        """Titles answered "no", so a later round does not ask again."""
        return self._read_json(self.rejected_path, {})

    def save_rejected(self, rejected: Dict[str, Any]) -> None:
        self._write_json(self.rejected_path, rejected)

    def load_accepted(self) -> list:
        """Everything ever accepted in discovery - the taste memory.

        Unlike the queue this is never cleared on send, so later rounds still
        know what you like.
        """
        return self._read_json(self.accepted_path, [])

    def save_accepted(self, accepted: list) -> None:
        self._write_json(self.accepted_path, accepted)

    def accepted_ids(self) -> set:
        """Simkl IDs ever accepted in discovery.

        This is the durable "you already answered yes to this" record: the
        queue is emptied on send and the library cache can be up to a day
        stale, so without this a title you just imported gets offered again.
        """
        ids = set()
        for raw in self.load_accepted():
            simkl = (raw.get("ids") or {}).get("simkl")
            if simkl:
                ids.add(str(simkl))
        return ids

    def load_detail_cache(self) -> Dict[str, Any]:
        """Overview/genres per Simkl ID. Titles do not change, so this never expires."""
        return self._read_json(self.detail_cache_path, {})

    def save_detail_cache(self, cache: Dict[str, Any]) -> None:
        self._write_json(self.detail_cache_path, cache)

    def load_genre_cache(self) -> Dict[str, Any]:
        return self._read_json(self.genre_cache_path, {})

    def save_genre_cache(self, cache: Dict[str, Any]) -> None:
        self._write_json(self.genre_cache_path, cache)

    def load_library(self) -> Dict[str, Any]:
        return self._read_json(self.library_path, {})

    def save_library(self, library: Dict[str, Any]) -> None:
        self._write_json(self.library_path, library)

    def invalidate_library(self) -> None:
        """Drop the cached library - call after writing to the account."""
        if self.library_path.exists():
            try:
                self.library_path.unlink()
            except OSError:
                pass
