"""Одноразовые коды подтверждения в Redis (регистрация, сброс и смена пароля)."""

from app.config import settings
from app.redis import get_redis
from app.security import generate_code

MAX_ATTEMPTS = 5


def _key(purpose: str, subject: str) -> str:
    return f"code:{purpose}:{subject.lower()}"


async def issue_code(purpose: str, subject: str) -> str:
    code = generate_code()
    key = _key(purpose, subject)
    await get_redis().hset(key, mapping={"code": code, "attempts": 0})
    await get_redis().expire(key, settings.code_ttl_seconds)
    return code


async def check_code(purpose: str, subject: str, code: str) -> bool:
    key = _key(purpose, subject)
    data = await get_redis().hgetall(key)
    if not data:
        return False
    if data["code"] == code.strip():
        await get_redis().delete(key)
        return True
    attempts = await get_redis().hincrby(key, "attempts", 1)
    if attempts >= MAX_ATTEMPTS:
        await get_redis().delete(key)
    return False
