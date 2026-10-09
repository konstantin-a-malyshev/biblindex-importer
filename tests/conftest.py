"""Shared fixtures: a fake clock and an in-memory fake of the BiblIndex API."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
import responses

from biblindex_importer.client import BiblIndexClient
from biblindex_importer.config import Settings
from biblindex_importer.rate_limiter import RateLimiter

BASE = "https://biblindex.test"


class FakeClock:
    """Monotonic clock whose sleep() just advances time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeApi:
    """Serves resources and Hydra collections the way BiblIndex does.

    Collection items may carry a private ``_f`` dict of filter values; a request
    parameter whose name appears in ``_f`` must match it. ``ignored_params``
    simulates the server silently dropping a filter.
    """

    def __init__(self, rsps: responses.RequestsMock, base: str = BASE) -> None:
        self.base = base
        self.resources: dict[str, dict[str, Any]] = {}
        self.collections: dict[str, list[dict[str, Any]]] = {}
        self.queued: dict[str, list[tuple[int, dict[str, str], str]]] = defaultdict(list)
        self.ignored_params: set[str] = set()
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.token_requests: list[dict[str, str]] = []
        self.token_counter = 0
        rsps.add_callback(responses.POST, f"{base}/api/token", callback=self._token)
        rsps.add_callback(
            responses.GET, re.compile(re.escape(base) + r"/api/.*"), callback=self._get
        )

    # -- setup ------------------------------------------------------------------

    def add(self, *items: dict[str, Any]) -> None:
        for item in items:
            self.resources[item["@id"]] = item

    def add_collection(self, path: str, items: list[dict[str, Any]]) -> None:
        self.collections[path] = items
        self.add(*items)

    def queue(self, path: str, status: int, body: str = "{}", **headers: str) -> None:
        """Answer the next GET of ``path`` with this response instead."""
        self.queued[path].append((status, headers, body))

    def calls_to(self, path: str) -> list[tuple[str, dict[str, str], dict[str, str]]]:
        return [c for c in self.calls if c[0] == path]

    # -- handlers -----------------------------------------------------------------

    def _token(self, request):
        self.token_requests.append(dict(parse_qsl(request.body)))
        self.token_counter += 1
        body = {"access_token": f"token-{self.token_counter}", "expires_in": 3600}
        return 200, {"Content-Type": "application/json"}, json.dumps(body)

    def _get(self, request):
        parts = urlsplit(request.url)
        path = parts.path
        params = dict(parse_qsl(parts.query))
        self.calls.append((path, params, dict(request.headers)))
        if self.queued[path]:
            status, headers, body = self.queued[path].pop(0)
            return status, headers, body
        if path in self.collections:
            return 200, {"Content-Type": "application/ld+json"}, json.dumps(
                self._page(path, params)
            )
        if path in self.resources:
            return 200, {"Content-Type": "application/ld+json"}, json.dumps(
                _public(self.resources[path])
            )
        return 404, {"Content-Type": "application/json"}, json.dumps({"detail": "Not Found"})

    def _page(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        items = [i for i in self.collections[path] if self._matches(i, params)]
        per_page = int(params.get("itemsPerPage", 30))
        page = int(params.get("page", 1))
        chunk = items[(page - 1) * per_page : page * per_page]
        body: dict[str, Any] = {
            "@id": path,
            "@type": "Collection",
            "totalItems": len(items),
            "member": [_public(i) for i in chunk],
            "view": {"@id": f"{path}?page={page}"},
        }
        if page * per_page < len(items):
            nxt = {k: v for k, v in params.items() if k != "page"}
            nxt["page"] = str(page + 1)
            body["view"]["next"] = f"{path}?{urlencode(sorted(nxt.items()))}"
        return body

    def _matches(self, item: dict[str, Any], params: dict[str, str]) -> bool:
        filters = item.get("_f", {})
        return all(
            str(filters[k]) == v
            for k, v in params.items()
            if k in filters and k not in self.ignored_params
        )


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k != "_f"}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        base_url=BASE,
        client_id="id",
        client_secret="secret",
        min_interval=1.0,
        public_min_interval=0.5,
    )


@pytest.fixture
def rsps():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def api(rsps) -> FakeApi:
    return FakeApi(rsps)


@pytest.fixture
def make_client(settings, clock):
    def make(cache=None, **kwargs) -> BiblIndexClient:
        return BiblIndexClient(
            kwargs.pop("settings", settings),
            cache=cache,
            auth_limiter=RateLimiter(1.0, clock=clock, sleep=clock.sleep, wall_clock=clock),
            public_limiter=RateLimiter(0.5, clock=clock, sleep=clock.sleep, wall_clock=clock),
            sleep=clock.sleep,
            **kwargs,
        )

    return make
