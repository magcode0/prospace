"""Очередь задач с таймерами: Celery + Redis.

  celery -A app.worker.celery_app worker -B -l info
"""

import asyncio
from collections.abc import Awaitable, Callable

from celery import Celery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import worker_session
from app.redis import set_redis

celery = Celery("prospace", broker=settings.celery_broker_url)
celery.conf.update(
    timezone="UTC",
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={"tick": {"task": "prospace.tick", "schedule": 60.0}},
)


def run(job: Callable[[AsyncSession], Awaitable[None]]) -> None:
    async def wrapper() -> None:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        set_redis(client)
        try:
            async with worker_session() as session:
                await job(session)
        finally:
            set_redis(None)
            await client.aclose()

    asyncio.run(wrapper())


@celery.task(name="prospace.tick")
def tick() -> None:
    from app.services.scheduler import tick as do_tick

    run(do_tick)


@celery.task(name="prospace.expire_offer")
def expire_offer(offer_id: int) -> None:
    from app.models import Offer
    from app.services import orders
    from app.services.notifications import commit_and_push

    async def job(session: AsyncSession) -> None:
        offer = await session.get(Offer, offer_id)
        if offer and await orders.expire_offer(session, offer):
            await commit_and_push(session)

    run(job)


@celery.task(name="prospace.moderate_order")
def moderate_order(order_id: int) -> None:
    from app.models import Order, OrderStatus
    from app.services import moderation, orders
    from app.services.notifications import commit_and_push

    async def job(session: AsyncSession) -> None:
        order = await session.get(Order, order_id)
        if not order or order.status != OrderStatus.moderation:
            return
        verdict = await moderation.moderate(order)
        if verdict.decision == "approve":
            offers = await orders.publish_order(session, order)
        elif verdict.decision == "reject":
            await orders.reject_moderation(session, order, verdict.reason)
            offers = []
        else:
            order.moderation_note = verdict.reason  # ждёт ручной проверки в /staff/moderation
            offers = []
        await commit_and_push(session)
        schedule_expiry(offers)

    run(job)


def schedule_expiry(offers) -> None:
    """Точный таймер 2 часа на каждое предложение (тик раз в минуту — страховка)."""
    for offer in offers:
        expire_offer.apply_async((offer.id,), eta=offer.expires_at)
