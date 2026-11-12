"""The redis-backed TokenCache that lets MCP instances behind one redis share the broker's
single live access token."""
import pytest

fakeredis = pytest.importorskip("fakeredis")

from pytossinvest_mcp.token_cache import RedisTokenCache

TOKEN = "access-token-0123456789"


@pytest.fixture
def r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _cache(r, secret="client-secret"):
    return RedisTokenCache(r, client_id="cid", client_secret=secret)


def test_another_instance_loads_what_one_saved(r):
    _cache(r).save(TOKEN, 120.0)

    token, ttl_sec = _cache(r).load()

    assert token == TOKEN
    assert 119.0 < ttl_sec <= 120.0


def test_nothing_saved_loads_nothing(r):
    assert _cache(r).load() is None


def test_the_token_is_not_readable_from_redis_alone(r):
    # redis read access must not amount to brokerage API access
    _cache(r).save(TOKEN, 120.0)

    stored = [r.get(key) for key in r.keys("*")]

    assert stored and all(TOKEN not in value for value in stored)
    assert _cache(r, secret="another-secret").load() is None


def test_discard_removes_only_the_token_it_was_given(r):
    cache = _cache(r)
    cache.save(TOKEN, 120.0)

    cache.discard("an-older-token")
    assert cache.load() is not None

    cache.discard(TOKEN)
    assert cache.load() is None


def test_lock_is_held_in_redis_for_the_block(r):
    cache = _cache(r)

    with cache.lock():
        held = r.keys("lock:*")

    assert held
    assert r.keys("lock:*") == []


def test_an_unreachable_redis_degrades_to_no_sharing():
    # the cache is an optimisation: without it each instance issues its own token, as before
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    nothing_listens_here = redis.Redis(
        host="127.0.0.1", port=1, socket_connect_timeout=0.2, decode_responses=True,
        retry=Retry(NoBackoff(), 0),  # redis-py's default retries would take seconds per call
    )
    cache = _cache(nothing_listens_here)

    with cache.lock():
        assert cache.load() is None
        cache.save(TOKEN, 120.0)
        cache.discard(TOKEN)


def test_an_entry_without_an_expiry_is_not_loaded(r):
    # the expiry is what bounds how long a token is trusted; never adopt one without it
    cache = _cache(r)
    cache.save(TOKEN, 120.0)
    for key in r.keys("oauth:token:*"):
        r.persist(key)

    assert cache.load() is None
