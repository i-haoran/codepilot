import json
import hashlib
from typing import Optional

from app.core.config import settings

_memory_cache: dict = {}


class CacheService:
    """
    缓存服务
    优先使用Redis，不可用时回退到内存缓存
    """

    def __init__(self):
        self.redis = None
        self.ttl = settings.CACHE_TTL
        try:
            import redis.asyncio as aioredis
            self.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception:
            pass

    def _make_key(self, message: str) -> str:
        hash_obj = hashlib.sha256(message.encode())
        return f"cache:{hash_obj.hexdigest()}"

    async def get(self, message: str) -> Optional[str]:
        key = self._make_key(message)
        if self.redis:
            try:
                cached = await self.redis.get(key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass
        cached = _memory_cache.get(key)
        if cached:
            return cached
        return None

    async def set(self, message: str, response: str):
        key = self._make_key(message)
        if self.redis:
            try:
                await self.redis.setex(key, self.ttl, json.dumps(response, ensure_ascii=False))
                return
            except Exception:
                pass
        _memory_cache[key] = response

    async def delete(self, message: str):
        key = self._make_key(message)
        _memory_cache.pop(key, None)
        if self.redis:
            try:
                await self.redis.delete(key)
            except Exception:
                pass

    async def clear(self):
        _memory_cache.clear()
        if self.redis:
            try:
                await self.redis.flushdb()
            except Exception:
                pass
