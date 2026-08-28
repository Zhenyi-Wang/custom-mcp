"""进程内 TTL 缓存。

FastMCP 在单事件循环中运行,无并发写竞争,不加锁。
个人场景用 dict + TTL 即可,无需 LFU 等复杂淘汰策略。
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any


class TTLCache:
    """带 TTL 与容量上限的缓存;超容量时先清过期项,再淘汰最旧写入。"""

    def __init__(
        self,
        maxsize: int = 256,
        ttl: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        ts, value = item
        if self._clock() - ts > self._ttl:
            del self._data[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        now = self._clock()
        self._data[key] = (now, value)
        self._data.move_to_end(key)
        expired = [k for k, (ts, _) in self._data.items() if now - ts > self._ttl]
        for k in expired:
            del self._data[k]
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)
