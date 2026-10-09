import json
from dataclasses import replace

import pytest
import requests

from biblindex_importer.cache import ResponseCache
from biblindex_importer.client import (
    ApiError,
    AuthenticationError,
    CloudflareChallengeError,
    PermissionDeniedError,
    RateLimitExceededError,
    hydra,
)

CHALLENGE_HTML = (
    "<html><head><title>Just a moment...</title></head>"
    "<script>window._cf_chl_opt={}</script></html>"
)


def quotation(n):
    return {"@id": f"/api/quotations/{n}", "id": n}


def test_token_is_fetched_once_and_sent_as_bearer(api, make_client):
    api.add(quotation(1), quotation(2))
    client = make_client()
    client.get("/api/quotations/1")
    client.get("/api/quotations/2")
    assert len(api.token_requests) == 1
    assert api.token_requests[0] == {
        "grant_type": "client_credentials",
        "client_id": "id",
        "client_secret": "secret",
    }
    assert all(c[2]["Authorization"] == "Bearer token-1" for c in api.calls)


def test_password_grant_when_username_configured(api, make_client, settings):
    api.add(quotation(1))
    client = make_client(settings=replace(settings, username="me", password="pw"))
    client.get("/api/quotations/1")
    assert api.token_requests[0]["grant_type"] == "password"
    assert api.token_requests[0]["username"] == "me"


def test_rejected_client_credentials_suggest_password_grant(rsps, make_client):
    rsps.add(
        "POST",
        "https://biblindex.test/api/token",
        status=401,
        json={"error": "invalid_client", "error_description": "Client authentication failed"},
    )
    with pytest.raises(AuthenticationError, match="BIBLINDEX_USERNAME"):
        make_client().get("/api/quotations/1")


def test_rejected_password_grant_has_no_username_hint(rsps, make_client, settings):
    rsps.add(
        "POST",
        "https://biblindex.test/api/token",
        status=401,
        json={"error": "invalid_grant", "error_description": "Invalid credentials"},
    )
    client = make_client(settings=replace(settings, username="me", password="wrong"))
    with pytest.raises(AuthenticationError) as exc_info:
        client.get("/api/quotations/1")
    assert "Invalid credentials" in str(exc_info.value)
    assert "BIBLINDEX_USERNAME" not in str(exc_info.value)


def test_requests_identify_the_tool_not_python_requests(api, make_client):
    api.add(quotation(1), {"@id": "/api/books/50"})
    client = make_client()
    client.get("/api/quotations/1")
    client.get("/api/books/50", auth=False)
    agents = {c[2]["User-Agent"] for c in api.calls}
    assert len(agents) == 1
    (agent,) = agents
    assert agent.startswith("biblindex-importer/")
    assert "python-requests" not in agent


def test_public_requests_send_no_token(api, make_client):
    api.add({"@id": "/api/books/50", "name": "John"})
    make_client().get("/api/books/50", auth=False)
    assert api.token_requests == []
    assert "Authorization" not in api.calls[0][2]


def test_missing_credentials_raise(api, make_client, settings):
    client = make_client(settings=replace(settings, client_id="", client_secret=""))
    with pytest.raises(AuthenticationError):
        client.get("/api/quotations/1")


def test_locale_is_sent(api, make_client):
    api.add(quotation(1))
    make_client().get("/api/quotations/1")
    assert api.calls[0][1]["locale"] == "en"


def test_429_is_retried_after_retry_after(api, make_client, clock):
    api.add(quotation(1))
    api.queue("/api/quotations/1", 429, **{"Retry-After": "5"})
    client = make_client()
    assert client.get("/api/quotations/1")["id"] == 1
    assert client.auth_limiter.stats.throttled == 1
    assert 5 in clock.sleeps
    assert client.auth_limiter.min_interval > 1.0  # slowed down for the rest of the run


def test_persistent_429_aborts(api, make_client):
    for _ in range(10):
        api.queue("/api/quotations/1", 429, **{"Retry-After": "1"})
    client = make_client(max_consecutive_throttles=3)
    with pytest.raises(RateLimitExceededError):
        client.get("/api/quotations/1")
    assert len(api.calls) == 4


def test_rate_limit_headers_pace_the_next_request(api, make_client, clock):
    api.add(quotation(1), quotation(2))
    api.queue(
        "/api/quotations/1",
        200,
        json.dumps(quotation(1)),
        **{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9"},
    )
    client = make_client()
    client.get("/api/quotations/1")
    client.get("/api/quotations/2")
    assert clock.sleeps[-1] == pytest.approx(2)  # reset of 9 s, capped at 2 s


def test_expired_token_is_refreshed_on_401(api, make_client):
    api.add(quotation(1))
    api.queue("/api/quotations/1", 401, '{"detail": "Expired"}')
    client = make_client()
    assert client.get("/api/quotations/1")["id"] == 1
    assert len(api.token_requests) == 2
    assert api.calls[-1][2]["Authorization"] == "Bearer token-2"


def test_403_explains_missing_permission(api, make_client):
    api.queue(
        "/api/quotations",
        403,
        '{"detail": "Access Denied."}',
        **{"Content-Type": "application/problem+json"},
    )
    with pytest.raises(PermissionDeniedError, match="ROLE_API_CLIENT"):
        make_client().get("/api/quotations")


def test_server_errors_are_retried_with_backoff(api, make_client, clock):
    api.add(quotation(1))
    api.queue("/api/quotations/1", 522, "")
    api.queue("/api/quotations/1", 503, "")
    assert make_client().get("/api/quotations/1")["id"] == 1
    assert 2.0 in clock.sleeps and 4.0 in clock.sleeps


def test_server_errors_give_up_eventually(api, make_client):
    for _ in range(10):
        api.queue("/api/quotations/1", 500, "")
    with pytest.raises(ApiError):
        make_client(max_retries=2).get("/api/quotations/1")


def test_network_errors_are_retried(rsps, make_client, clock):
    rsps.add("GET", "https://biblindex.test/api/books/1", body=requests.ConnectionError("boom"))
    rsps.add("GET", "https://biblindex.test/api/books/1", json={"@id": "/api/books/1"})
    assert make_client().get("/api/books/1", auth=False)["@id"] == "/api/books/1"


def test_cloudflare_challenge_on_public_request_retries_with_token(api, make_client):
    api.add({"@id": "/api/authors/90", "name": "Augustine"})
    api.queue("/api/authors/90", 403, CHALLENGE_HTML, **{"Content-Type": "text/html"})
    client = make_client()
    assert client.get("/api/authors/90", auth=False)["name"] == "Augustine"
    assert "Authorization" not in api.calls[0][2]
    assert api.calls[1][2]["Authorization"].startswith("Bearer")


def test_persistent_cloudflare_challenge_raises(api, make_client, clock):
    for _ in range(5):
        api.queue(
            "/api/quotations/1",
            403,
            CHALLENGE_HTML,
            **{"Content-Type": "text/html", "CF-RAY": "abc123-FRA"},
        )
    with pytest.raises(CloudflareChallengeError, match="CF-RAY abc123-FRA"):
        make_client().get("/api/quotations/1")
    assert clock.sleeps.count(30.0) == 1 and clock.sleeps.count(60.0) == 1


def test_iter_collection_follows_next_links(api, make_client):
    api.add_collection("/api/quotations", [quotation(n) for n in range(1, 251)])
    pages = []
    items = list(
        make_client().iter_collection(
            "/api/quotations", {"bible": 8}, on_page=lambda p, t: pages.append((p, t))
        )
    )
    assert [i["id"] for i in items] == list(range(1, 251))
    assert pages == [(1, 250), (2, 250), (3, 250)]
    assert all(c[1]["itemsPerPage"] == "100" for c in api.calls)
    assert all(c[1]["bible"] == "8" for c in api.calls)


def test_iter_collection_limit_stops_early(api, make_client):
    api.add_collection("/api/quotations", [quotation(n) for n in range(1, 251)])
    items = list(make_client().iter_collection("/api/quotations", limit=5))
    assert len(items) == 5
    assert len(api.calls) == 1


def test_total_items_uses_a_one_item_page(api, make_client):
    api.add_collection("/api/works", [{"@id": f"/api/works/{n}"} for n in range(42)])
    assert make_client().total_items("/api/works", auth=False) == 42
    assert api.calls[0][1]["itemsPerPage"] == "1"


def test_cache_avoids_repeat_requests(api, make_client):
    api.add(quotation(1))
    cache = ResponseCache(":memory:")
    client = make_client(cache=cache)
    client.get("/api/quotations/1")
    assert client.is_cached("/api/quotations/1")
    again = make_client(cache=cache)
    assert again.get("/api/quotations/1")["id"] == 1
    assert len(api.calls) == 1
    assert again.cache_hits == 1


def test_cache_respects_ttl(api, make_client, monkeypatch):
    api.add(quotation(1))
    cache = ResponseCache(":memory:", ttl_seconds=10)
    make_client(cache=cache).get("/api/quotations/1")
    import biblindex_importer.cache as cache_module

    real_time = cache_module.time.time
    monkeypatch.setattr(cache_module.time, "time", lambda: real_time() + 60)
    make_client(cache=cache).get("/api/quotations/1")
    assert len(api.calls) == 2


def test_hydra_prefixed_keys():
    assert hydra({"hydra:member": [1]}, "member") == [1]
    assert hydra({"member": [2]}, "member") == [2]
    assert hydra({}, "member", []) == []


def test_on_response_fires_for_every_network_response(api, make_client):
    api.add(quotation(1))
    api.queue("/api/quotations/1", 429, **{"Retry-After": "1"})
    seen = []
    client = make_client(cache=ResponseCache(":memory:"), on_response=seen.append)
    client.get("/api/quotations/1")  # token + 429 + 200
    client.get("/api/quotations/1")  # cache hit: nothing sent
    assert [r.status_code for r in seen] == [200, 429, 200]
