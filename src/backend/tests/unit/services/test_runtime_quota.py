from uuid import uuid4

import fakeredis.aioredis as fakeredis_aio
import pytest
from langflow.services.database.models.api_key.model import ApiKey
from langflow.services.runtime_quota import RuntimeQuotaExceededError, RuntimeQuotaService


async def test_runtime_quota_counters_are_per_key_and_release_concurrency():
    client = fakeredis_aio.FakeRedis()
    service = RuntimeQuotaService(client)
    api_key = ApiKey(
        api_key="encrypted",  # pragma: allowlist secret
        user_id=uuid4(),
        rate_limit_per_minute=2,
        daily_quota=2,
        max_concurrent_runs=1,
    )

    await service.acquire(api_key)
    with pytest.raises(RuntimeQuotaExceededError, match="concurrency"):
        await service.acquire(api_key)

    await service.release(api_key)
    await service.acquire(api_key)
    await service.release(api_key)
    with pytest.raises(RuntimeQuotaExceededError, match="rate"):
        await service.acquire(api_key)

    other_key = api_key.model_copy(update={"id": uuid4()})
    await service.acquire(other_key)
    await service.release(other_key)
    await client.aclose()
