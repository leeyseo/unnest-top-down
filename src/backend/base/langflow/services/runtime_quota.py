"""Atomic Redis counters for on-premise runtime API keys."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from redis.asyncio import StrictRedis
from redis.exceptions import RedisError, WatchError

from langflow.services.deps import get_settings_service

if TYPE_CHECKING:
    from langflow.services.database.models.api_key.model import ApiKey


class RuntimeQuotaExceededError(Exception):
    def __init__(self, limit: str) -> None:
        super().__init__(limit)
        self.limit = limit


class RuntimeQuotaService:
    def __init__(self, client: StrictRedis) -> None:
        self.client = client

    @staticmethod
    def _keys(api_key: ApiKey) -> tuple[str, str, str]:
        now = datetime.now(timezone.utc)
        prefix = f"unnest:runtime:api-key:{api_key.id}"
        return (
            f"{prefix}:minute:{now:%Y%m%d%H%M}",
            f"{prefix}:day:{now:%Y%m%d}",
            f"{prefix}:concurrent",
        )

    async def acquire(self, api_key: ApiKey) -> None:
        keys = self._keys(api_key)
        limits = (api_key.rate_limit_per_minute, api_key.daily_quota, api_key.max_concurrent_runs)
        names = ("rate", "daily", "concurrency")
        for _attempt in range(10):
            try:
                async with self.client.pipeline(transaction=True) as pipe:
                    await pipe.watch(*keys)
                    current = [int(value or 0) for value in await pipe.mget(keys)]
                    if exceeded := next(
                        (name for name, count, limit in zip(names, current, limits, strict=True) if count >= limit),
                        None,
                    ):
                        await pipe.unwatch()
                        raise RuntimeQuotaExceededError(exceeded)
                    pipe.multi()
                    for key, ttl in zip(keys, (120, 172_800, 86_400), strict=True):
                        pipe.incr(key)
                        pipe.expire(key, ttl)
                    await pipe.execute()
                    return
            except WatchError:
                continue
            except RedisError as exc:
                msg = "Runtime quota store is unavailable"
                raise RuntimeError(msg) from exc
        msg = "Runtime quota counter contention is too high"
        raise RuntimeError(msg)

    async def release(self, api_key: ApiKey) -> None:
        key = self._keys(api_key)[2]
        for _attempt in range(10):
            try:
                async with self.client.pipeline(transaction=True) as pipe:
                    await pipe.watch(key)
                    current = int(await pipe.get(key) or 0)
                    pipe.multi()
                    if current > 1:
                        pipe.decr(key)
                    else:
                        pipe.delete(key)
                    await pipe.execute()
                    return
            except WatchError:
                continue
            except RedisError:
                return


_runtime_quota_service: RuntimeQuotaService | None = None


def get_runtime_quota_service() -> RuntimeQuotaService:
    global _runtime_quota_service  # noqa: PLW0603
    if _runtime_quota_service is None:
        settings = get_settings_service().settings
        client = (
            StrictRedis.from_url(settings.redis_url)
            if settings.redis_url
            else StrictRedis(host=settings.redis_host, port=settings.redis_port, db=settings.redis_db)
        )
        _runtime_quota_service = RuntimeQuotaService(client)
    return _runtime_quota_service
