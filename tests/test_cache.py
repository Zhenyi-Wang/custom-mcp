from src.tools.cache import TTLCache


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_get_miss_returns_none():
    cache = TTLCache()
    assert cache.get("k") is None


def test_set_then_get():
    cache = TTLCache()
    cache.set("k", {"v": 1})
    assert cache.get("k") == {"v": 1}


def test_expired_entry_returns_none():
    clock = FakeClock()
    cache = TTLCache(ttl=10.0, clock=clock)
    cache.set("k", "v")
    clock.now += 11.0
    assert cache.get("k") is None


def test_unexpired_entry_survives():
    clock = FakeClock()
    cache = TTLCache(ttl=10.0, clock=clock)
    cache.set("k", "v")
    clock.now += 9.0
    assert cache.get("k") == "v"


def test_maxsize_evicts_oldest():
    clock = FakeClock()
    cache = TTLCache(maxsize=2, ttl=100.0, clock=clock)
    cache.set("a", 1)
    clock.now += 1
    cache.set("b", 2)
    clock.now += 1
    cache.set("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
