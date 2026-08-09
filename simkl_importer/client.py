"""Rate-limited HTTP client for the Simkl API.

Simkl's rules that this enforces:
  * every request carries client_id / app-name / app-version query params,
    a descriptive User-Agent and the simkl-api-key header;
  * POST is capped at 1 request per second per client;
  * 429/5xx are retried with backoff, honouring Retry-After.
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any, Dict, Optional

import requests

from .config import (
    API_BASE,
    APP_NAME,
    APP_VERSION,
    DEFAULT_DAILY_LIMIT,
    USER_AGENT,
    Credentials,
    Store,
)

# 1 POST/sec is the documented ceiling; leave a little headroom for clock skew.
POST_MIN_INTERVAL = 1.1
GET_MIN_INTERVAL = 0.25


class SimklError(RuntimeError):
    """Any non-retryable API failure."""


class AuthError(SimklError):
    """The access token is missing, revoked or rejected."""


class BudgetExceeded(SimklError):
    """Local guard against blowing through the app's daily request quota."""


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


class SimklClient:
    def __init__(
        self,
        credentials: Credentials,
        store: Store,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        max_retries: int = 4,
        timeout: int = 30,
        verbose: bool = False,
    ):
        self.credentials = credentials
        self.store = store
        self.daily_limit = daily_limit
        self.max_retries = max_retries
        self.timeout = timeout
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "simkl-api-key": credentials.client_id,
            }
        )
        self._post_limiter = RateLimiter(POST_MIN_INTERVAL)
        self._get_limiter = RateLimiter(GET_MIN_INTERVAL)

    # ------------------------------------------------------------------ core

    def _auth_headers(self, authenticated: bool) -> Dict[str, str]:
        if not authenticated:
            return {}
        if not self.credentials.access_token:
            raise AuthError("No access token yet - run the authentication step first.")
        return {"Authorization": f"Bearer {self.credentials.access_token}"}

    def _base_params(self) -> Dict[str, str]:
        return {
            "client_id": self.credentials.client_id,
            "app-name": APP_NAME,
            "app-version": APP_VERSION,
        }

    def _check_budget(self) -> None:
        used = self.store.requests_today()
        if self.daily_limit and used >= self.daily_limit:
            raise BudgetExceeded(
                f"Local daily budget reached ({used}/{self.daily_limit} requests today). "
                "Wait for the UTC day to roll over, or raise --daily-limit if your app "
                "has been approved for a higher quota."
            )

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Any] = None,
        authenticated: bool = False,
        base: str = API_BASE,
    ) -> Any:
        url = path if path.startswith("http") else f"{base}{path}"
        query = self._base_params()
        query.update(params or {})
        headers = self._auth_headers(authenticated)
        limiter = self._post_limiter if method.upper() == "POST" else self._get_limiter

        last_error = ""
        for attempt in range(self.max_retries + 1):
            self._check_budget()
            limiter.wait()
            self.store.record_requests(1)
            if self.verbose:
                print(f"    -> {method} {url}")
            try:
                response = self.session.request(
                    method,
                    url,
                    params=query,
                    data=json.dumps(payload) if payload is not None else None,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                self._backoff(attempt, last_error)
                continue

            status = response.status_code
            if status in (200, 201, 204):
                if not response.content:
                    return None
                try:
                    return response.json()
                except ValueError:
                    return response.text

            if status in (401, 403):
                raise AuthError(
                    f"{status} from {url}. The token or client_id was rejected - "
                    "re-run authentication (option 1 in the menu)."
                )
            if status == 404:
                return None
            if status == 429:
                retry_after = self._retry_after(response)
                last_error = f"429 rate limited (waiting {retry_after:.0f}s)"
                print(f"    ! {last_error}")
                time.sleep(retry_after)
                continue
            if 500 <= status < 600:
                last_error = f"{status} server error"
                self._backoff(attempt, last_error)
                continue

            raise SimklError(f"{status} from {url}: {response.text[:300]}")

        raise SimklError(f"Gave up on {method} {url} after {self.max_retries + 1} attempts ({last_error})")

    @staticmethod
    def _retry_after(response: requests.Response) -> float:
        raw = response.headers.get("Retry-After", "")
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            return 5.0

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = min(30.0, (2**attempt)) + random.uniform(0, 0.5)
        print(f"    ! {reason}; retrying in {delay:.1f}s")
        time.sleep(delay)

    # ------------------------------------------------------------- endpoints

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, payload: Any, **kwargs) -> Any:
        return self.request("POST", path, payload=payload, **kwargs)

    def search(self, media_type: str, query: str, limit: int = 5) -> list:
        """Text search. media_type is one of tv / movie / anime."""
        result = self.get(
            f"/search/{media_type}",
            params={"q": query, "page": 1, "limit": limit, "extended": "full"},
        )
        return result if isinstance(result, list) else []

    def search_by_imdb(self, imdb_id: str) -> list:
        result = self.get("/search/id", params={"imdb": imdb_id})
        return result if isinstance(result, list) else []

    def genre_titles(
        self,
        section: str,
        genre: str = "all",
        sort: str = "popular-this-month",
        limit: int = 20,
        page: int = 1,
    ) -> list:
        """Popular titles for a genre.

        `section` is tv / movies / anime and the path mirrors the filter URLs on
        simkl.com, e.g. /tv/genres/drama/all-types/all-countries/all-networks/
        all-years/popular-this-month
        """
        if section == "tv":
            path = f"/tv/genres/{genre}/all-types/all-countries/all-networks/all-years/{sort}"
        elif section == "anime":
            path = f"/anime/genres/{genre}/all-types/all-networks/all-years/{sort}"
        else:
            path = f"/movies/genres/{genre}/all-types/all-countries/all-years/{sort}"
        result = self.get(
            path,
            params={"page": page, "limit": limit, "extended": "title,genres"},
        )
        return result if isinstance(result, list) else []

    def trending(self, section: str, timeframe: str = "month") -> list:
        """Pre-built trending files on the Simkl CDN (no API key, no quota)."""
        url = f"https://data.simkl.in/discover/trending/{section}/{timeframe}_100.json"
        try:
            response = self.session.get(url, timeout=self.timeout, headers={"User-Agent": USER_AGENT})
            if response.status_code != 200:
                return []
            data = response.json()
        except (requests.RequestException, ValueError):
            return []
        return data if isinstance(data, list) else []

    def library_ids(self, media_type: str) -> list:
        """The user's existing watchlist for one type, IDs only.

        `extended=simkl_ids_only` keeps this to the minimum payload Simkl asks
        for, and we call the three types sequentially rather than in parallel.
        """
        result = self.get(
            f"/sync/all-items/{media_type}",
            params={"extended": "simkl_ids_only"},
            authenticated=True,
        )
        return result if isinstance(result, (list, dict)) else []

    def add_to_history(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self.post("/sync/history", payload=payload, authenticated=True)
        return result if isinstance(result, dict) else {}
