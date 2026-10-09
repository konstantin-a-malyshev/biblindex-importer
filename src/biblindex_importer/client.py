"""HTTP client for the BiblIndex REST API (JSON-LD / Hydra collections).

Handles OAuth2 tokens, rate-limit pacing, retries, response caching and
pagination. It knows nothing about books or quotations; see ``exporter``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import requests

from . import __version__
from .cache import ResponseCache
from .config import Settings
from .rate_limiter import RateLimiter

log = logging.getLogger(__name__)

USER_AGENT = f"biblindex-importer/{__version__} (BiblIndex API client)"
TOKEN_PATH = "/api/token"
# Renew the token this many seconds before it expires.
TOKEN_EXPIRY_MARGIN = 60
DEFAULT_TOKEN_LIFETIME = 3600


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthenticationError(ApiError):
    pass


class PermissionDeniedError(ApiError):
    pass


class RateLimitExceededError(ApiError):
    pass


class CloudflareChallengeError(ApiError):
    pass


def is_cloudflare_challenge(response: requests.Response) -> bool:
    """True when Cloudflare answered with a browser check instead of the API."""
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if response.status_code not in (403, 429, 503):
        return False
    if "text/html" not in response.headers.get("Content-Type", ""):
        return False
    text = response.text[:5000]
    return "_cf_chl_opt" in text or "Just a moment" in text or "challenge-platform" in text


def hydra(data: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Read a Hydra key whether or not the server prefixes it with ``hydra:``."""
    if key in data:
        return data[key]
    return data.get(f"hydra:{key}", default)


class BiblIndexClient:
    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
        cache: ResponseCache | None = None,
        auth_limiter: RateLimiter | None = None,
        public_limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
        max_consecutive_throttles: int = 5,
        timeout: float = 60.0,
        on_response: Callable[[requests.Response], None] | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        # Assign, don't setdefault: requests.Session already carries
        # "python-requests/x.y", which BiblIndex's Cloudflare rules challenge.
        self.session.headers["User-Agent"] = USER_AGENT
        self.cache = cache
        self.auth_limiter = auth_limiter or RateLimiter(
            settings.min_interval, max_reset_wait=settings.max_reset_wait
        )
        self.public_limiter = public_limiter or RateLimiter(
            settings.public_min_interval, max_reset_wait=settings.max_reset_wait
        )
        self._sleep = sleep
        self.max_retries = max_retries
        self.max_consecutive_throttles = max_consecutive_throttles
        self.timeout = timeout
        # Called after every response received over the network (not cache hits),
        # e.g. to show progress.
        self.on_response = on_response
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.cache_hits = 0

    # -- URLs -----------------------------------------------------------------

    def url(self, path_or_iri: str) -> str:
        if path_or_iri.startswith(("http://", "https://")):
            return path_or_iri
        return f"{self.settings.base_url}/{path_or_iri.lstrip('/')}"

    def _cache_key(self, url: str, params: Mapping[str, Any] | None) -> str:
        prepared = requests.Request("GET", url, params=sorted((params or {}).items())).prepare()
        return prepared.url or url

    # -- Public API -----------------------------------------------------------

    def get(
        self,
        path_or_iri: str,
        params: Mapping[str, Any] | None = None,
        *,
        auth: bool = True,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """GET a resource or collection page. ``auth=False`` for public data."""
        url = self.url(path_or_iri)
        params = dict(params or {})
        params.setdefault("locale", self.settings.locale)
        key = self._cache_key(url, params)
        if use_cache and self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                return cached
        data = self._request(url, params, auth=auth)
        if self.cache is not None:
            self.cache.set(key, data)
        return data

    def is_cached(self, path_or_iri: str, params: Mapping[str, Any] | None = None) -> bool:
        if self.cache is None:
            return False
        params = dict(params or {})
        params.setdefault("locale", self.settings.locale)
        return self.cache.get(self._cache_key(self.url(path_or_iri), params)) is not None

    def iter_collection(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        auth: bool = True,
        items_per_page: int = 100,
        limit: int | None = None,
        on_page: Callable[[int, int | None], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every member of a paginated collection, following ``view.next``."""
        page_params: dict[str, Any] | None = {
            **(params or {}),
            "itemsPerPage": items_per_page,
            "page": 1,
        }
        next_ref: str | None = path
        yielded = 0
        page_number = 0
        while next_ref:
            data = self.get(next_ref, page_params, auth=auth)
            page_number += 1
            if on_page is not None:
                on_page(page_number, hydra(data, "totalItems"))
            for item in hydra(data, "member", []):
                yield item
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            view = hydra(data, "view", {}) or {}
            next_ref = hydra(view, "next")
            # The "next" IRI already carries every query parameter.
            page_params = None

    def total_items(
        self, path: str, params: Mapping[str, Any] | None = None, *, auth: bool = True
    ) -> int:
        """Collection size, at the cost of one request for a one-item page."""
        data = self.get(path, {**(params or {}), "itemsPerPage": 1, "page": 1}, auth=auth)
        return int(hydra(data, "totalItems", 0) or 0)

    # -- Internals ------------------------------------------------------------

    def _request(self, url: str, params: Mapping[str, Any], *, auth: bool) -> dict[str, Any]:
        consecutive_throttles = 0
        refreshed_token = False
        challenges = 0
        attempt = 0
        while True:
            limiter = self.auth_limiter if auth else self.public_limiter
            headers = {"Accept": "application/ld+json"}
            if auth:
                headers["Authorization"] = f"Bearer {self._access_token()}"
            limiter.wait()
            try:
                response = self.session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise ApiError(f"Network error for {url}: {exc}") from exc
                self._backoff(attempt, f"network error ({exc.__class__.__name__})")
                continue

            self._notify(response)
            limiter.update_from_headers(response.headers)
            status = response.status_code

            if is_cloudflare_challenge(response):
                if not auth and self.settings.has_credentials:
                    log.info("Cloudflare challenged an anonymous request; retrying with token.")
                    auth = True
                    continue
                challenges += 1
                if challenges > 2:
                    # A managed challenge needs a real browser; we never try to
                    # solve or evade it. The site operators must allow API traffic.
                    raise CloudflareChallengeError(
                        f"Cloudflare blocks {url} with a browser check "
                        f"(HTTP {status}, CF-RAY {response.headers.get('CF-RAY', 'n/a')}, "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')}). This is a setting on the "
                        "BiblIndex side; try again later, or send this line to the BiblIndex "
                        "team and ask them to exempt authenticated /api/ requests.",
                        status,
                    )
                wait = 30.0 * challenges
                log.warning("Cloudflare browser check on %s; waiting %.0fs.", url, wait)
                self._sleep(wait)
                continue

            if status == 429:
                consecutive_throttles += 1
                if consecutive_throttles > self.max_consecutive_throttles:
                    raise RateLimitExceededError(
                        f"Still rate limited after {self.max_consecutive_throttles} retries; "
                        "stopping so the account is not penalised further.",
                        status,
                    )
                limiter.register_throttled(response.headers)
                continue

            if status == 401 and auth and not refreshed_token:
                log.info("Access token rejected; requesting a new one.")
                self._token = None
                refreshed_token = True
                continue

            if status >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise ApiError(f"Server error {status} for {url}", status)
                self._backoff(attempt, f"server error {status}")
                continue

            if status == 401:
                raise AuthenticationError(f"Not authenticated for {url}: {_detail(response)}", 401)
            if status == 403:
                raise PermissionDeniedError(
                    f"Access denied to {url}: {_detail(response).rstrip('.')}. Corpus data "
                    "needs an API client account (ROLE_API_CLIENT), and some resources "
                    "also need a per-resource grant from a BiblIndex administrator.",
                    403,
                )
            if not response.ok:
                raise ApiError(f"HTTP {status} for {url}: {_detail(response)}", status)
            try:
                return response.json()
            except ValueError as exc:
                raise ApiError(f"Invalid JSON from {url}", status) from exc

    def _notify(self, response: requests.Response) -> None:
        if self.on_response is not None:
            self.on_response(response)

    def _backoff(self, attempt: int, reason: str) -> None:
        wait = min(2.0**attempt, 60.0)
        log.warning("%s; retry %d/%d in %.0fs.", reason, attempt, self.max_retries, wait)
        self._sleep(wait)

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - TOKEN_EXPIRY_MARGIN:
            return self._token
        if not self.settings.has_credentials:
            raise AuthenticationError(
                "BIBLINDEX_CLIENT_ID and BIBLINDEX_CLIENT_SECRET must be set in .env."
            )
        data = {
            "grant_type": "client_credentials",
            "client_id": self.settings.client_id,
            "client_secret": self.settings.client_secret,
        }
        if self.settings.username and self.settings.password:
            data.update(
                grant_type="password",
                username=self.settings.username,
                password=self.settings.password,
            )
        self.auth_limiter.wait()
        response = self.session.post(
            self.url(TOKEN_PATH),
            data=data,
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        self._notify(response)
        self.auth_limiter.update_from_headers(response.headers)
        if not response.ok:
            hint = ""
            if data["grant_type"] == "client_credentials":
                # BiblIndex API clients are normally set up for the password
                # grant; asking for another grant fails as "client authentication".
                hint = (
                    " BiblIndex API clients usually need the password grant: set "
                    "BIBLINDEX_USERNAME and BIBLINDEX_PASSWORD (your biblindex.org login)."
                )
            raise AuthenticationError(
                f"Could not obtain an access token (HTTP {response.status_code}): "
                f"{_detail(response)}.{hint}",
                response.status_code,
            )
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AuthenticationError("Token response did not contain access_token.")
        self._token = token
        self._token_expires_at = time.time() + float(
            payload.get("expires_in") or DEFAULT_TOKEN_LIFETIME
        )
        log.info("Obtained API access token.")
        return token


def _detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        for key in ("detail", "hydra:description", "description", "error_description", "title"):
            if payload.get(key):
                return str(payload[key])
    return str(payload)[:200]
