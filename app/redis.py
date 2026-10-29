from contextvars import ContextVar

from redis.asyncio import Redis

from app.config import settings

redis = Redis.from_url(settings.redis_url, decode_responses=True)

# Celery-задачи запускают собственный event loop, а соединения redis.asyncio привязаны
# к loop'у — поэтому воркер подставляет здесь свой клиент на время задачи.
_override: ContextVar[Redis | None] = ContextVar("redis_override", default=None)


def get_redis() -> Redis:
    return _override.get() or redis


def set_redis(client: Redis | None) -> None:
    _override.set(client)


def user_channel(user_id: int) -> str:
    return f"user:{user_id}"


def chat_channel(order_id: int, freelancer_id: int) -> str:
    return f"chat:{order_id}:{freelancer_id}"
