"""Периодический «тик» (Celery beat, раз в минуту): все таймеры биржи считаются от меток времени в БД,
поэтому тик идемпотентен и переживает рестарты воркера."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ACTIVE_WORK_STATUSES,
    Dispute,
    DisputeStatus,
    Offer,
    OfferStatus,
    Order,
    OrderStatus,
    User,
    utcnow,
)
from app.redis import get_redis
from app.services import matching, orders
from app.services.notifications import Kind, commit_and_push, notify

log = logging.getLogger(__name__)


async def expire_offers(session: AsyncSession, now: datetime) -> int:
    expired = (
        await session.execute(select(Offer).where(Offer.status == OfferStatus.sent, Offer.expires_at <= now))
    ).scalars().all()
    for offer in expired:
        await orders.expire_offer(session, offer, now)
    return len(expired)


async def refill_open_orders(session: AsyncSession, now: datetime) -> None:
    open_orders = (
        await session.execute(select(Order).where(Order.status == OrderStatus.open, Order.matching_active.is_(True)))
    ).scalars().all()
    for order in open_orders:
        await matching.fill_slots(session, order, now)


async def deadline_reminders(session: AsyncSession, now: datetime) -> None:
    soon = now + timedelta(hours=settings.deadline_reminder_hours)
    rows = (
        await session.execute(
            select(Order).where(
                Order.status.in_(ACTIVE_WORK_STATUSES),
                Order.reminder_sent.is_(False),
                Order.deadline <= soon,
                Order.deadline > now,
            )
        )
    ).scalars().all()
    for order in rows:
        order.reminder_sent = True
        text = f"До окончания срока заказа «{order.title}» осталось меньше {settings.deadline_reminder_hours} ч."
        notify(session, order.client, Kind.deadline_soon, text, f"/client/orders/{order.id}")
        notify(session, order.freelancer, Kind.deadline_soon, text, f"/freelancer/work/{order.id}")


async def overdue_orders(session: AsyncSession, now: datetime) -> None:
    """Срок вышел, а работа не принята — несогласие сторон: открываем окно спора."""
    rows = (
        await session.execute(
            select(Order).where(Order.status.in_(ACTIVE_WORK_STATUSES), Order.deadline <= now)
        )
    ).scalars().all()
    for order in rows:
        orders.open_dispute_window(order, now)
        notify(session, order.client, Kind.dispute, f"Срок заказа «{order.title}» истёк, работа не принята. Открыто окно спора на {settings.dispute_window_hours} ч.", f"/client/orders/{order.id}")
        notify(session, order.freelancer, Kind.work_declined, f"Срок заказа «{order.title}» истёк, работа не принята. В течение {settings.dispute_window_hours} ч можно обжаловать решение.", f"/freelancer/work/{order.id}")


async def close_dispute_windows(session: AsyncSession, now: datetime) -> None:
    rows = (
        await session.execute(
            select(Order).where(Order.status == OrderStatus.declined, Order.dispute_window_until <= now)
        )
    ).scalars().all()
    for order in rows:
        await orders.close_dispute_window(session, order)


async def escalate_disputes(session: AsyncSession, now: datetime) -> None:
    overdue = (
        await session.execute(select(Dispute).where(Dispute.status == DisputeStatus.open, Dispute.due_at <= now))
    ).scalars().all()
    if not overdue:
        return
    staff = (await session.execute(select(User).where(User.is_staff.is_(True)))).scalars().all()
    for dispute in overdue:
        if not await get_redis().set(f"dispute:escalated:{dispute.id}", 1, nx=True, ex=86400):
            continue
        for member in staff:
            notify(session, member, Kind.dispute, f"Просрочено рассмотрение спора #{dispute.id} по заказу «{dispute.order.title}».", "/staff/disputes")


async def tick(session: AsyncSession, now: datetime | None = None) -> None:
    now = now or utcnow()
    for step in (expire_offers, refill_open_orders, deadline_reminders, overdue_orders, close_dispute_windows, escalate_disputes):
        try:
            await step(session, now)
            await commit_and_push(session)
        except Exception:
            log.exception("Ошибка шага планировщика %s", step.__name__)
            await session.rollback()
