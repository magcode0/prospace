"""Алгоритм подбора исполнителей (v1: правила + очередь задач с таймерами, без ML).

1. Отбор кандидатов: категория, цена ≥ минималки исполнителя, статус «свободен»,
   был в сети ≤ 4 ч назад, ещё не получал/не отказывался от этого заказа, сейчас его рабочие часы.
2. Ранжирование: рейтинг, число выполненных заказов, недавность захода, частота реакции
   на предложения + небольшая случайная доля. Игнорирующие предложения опускаются ниже.
3. 10 слотов: 7 опытных + 3 новичка (новички ранжируются отдельно), с добором из другой группы.
4. Предложение живёт 2 часа; отказ/молчание/отказ заказчика освобождают слот, его сразу
   получает следующий кандидат.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    SLOT_STATUSES,
    BusyStatus,
    Offer,
    OfferStatus,
    Order,
    OrderStatus,
    Role,
    User,
    user_categories,
    utcnow,
)
from app.services.notifications import Kind, notify

W_RATING = 0.35
W_COMPLETED = 0.25
W_RECENCY = 0.15
W_REACTION = 0.20
W_RANDOM = 0.05
COMPLETED_CAP = 20


@dataclass
class Candidate:
    user: User
    score: float


def in_work_hours(user: User, now: datetime) -> bool:
    hour = now.astimezone(ZoneInfo(settings.timezone)).hour
    start, end = user.work_hours_start, user.work_hours_end
    if start == end:
        return True  # круглосуточно
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # смена через полночь


def reaction_rate(user: User) -> float:
    # сглаживание Лапласа: у новичка без истории — 0.5
    return (user.offers_reacted + 1) / (user.offers_received + 2)


def score(user: User, now: datetime, rng: random.Random | None = None) -> float:
    rng = rng or random
    hours_ago = max((now - user.last_seen_at).total_seconds() / 3600, 0)
    recency = max(0.0, 1 - hours_ago / settings.max_last_seen_hours)
    return (
        W_RATING * (user.rating / 5)
        + W_COMPLETED * min(user.completed_orders, COMPLETED_CAP) / COMPLETED_CAP
        + W_RECENCY * recency
        + W_REACTION * reaction_rate(user)
        + W_RANDOM * rng.random()
    )


def pick(
    experienced: list[Candidate],
    newbies: list[Candidate],
    active_experienced: int,
    active_newbies: int,
    free_slots: int,
) -> list[Candidate]:
    """Выбирает, кому отдать свободные слоты, соблюдая пропорцию 7 + 3 с добором."""
    experienced = sorted(experienced, key=lambda c: c.score, reverse=True)
    newbies = sorted(newbies, key=lambda c: c.score, reverse=True)

    want_exp = max(settings.match_experienced_slots - active_experienced, 0)
    want_new = max(settings.match_newbie_slots - active_newbies, 0)

    take_exp = experienced[: min(want_exp, free_slots)]
    take_new = newbies[: min(want_new, free_slots - len(take_exp))]
    chosen = take_exp + take_new

    # добор: не хватило новичков — берём опытных, и наоборот
    rest = free_slots - len(chosen)
    if rest > 0:
        extra_exp = experienced[len(take_exp) :]
        extra_new = newbies[len(take_new) :]
        chosen += (extra_exp + extra_new)[:rest]
    return chosen


async def _candidates(session: AsyncSession, order: Order, now: datetime) -> list[User]:
    already = select(Offer.freelancer_id).where(Offer.order_id == order.id)
    stmt = (
        select(User)
        .join(user_categories, user_categories.c.user_id == User.id)
        .where(
            user_categories.c.category_id == order.category_id,
            User.role == Role.freelancer,
            User.is_email_verified.is_(True),
            User.min_price <= order.price,
            User.busy_status == BusyStatus.free,
            User.last_seen_at >= now - timedelta(hours=settings.max_last_seen_hours),
            User.id != order.client_id,
            User.id.not_in(already),
        )
    )
    users = (await session.execute(stmt)).unique().scalars().all()
    return [u for u in users if in_work_hours(u, now)]


async def response_count(session: AsyncSession, order_id: int) -> int:
    return await session.scalar(
        select(func.count()).where(Offer.order_id == order_id, Offer.responded_at.is_not(None))
    ) or 0


async def stop_matching(session: AsyncSession, order: Order) -> None:
    order.matching_active = False
    pending = await session.execute(
        select(Offer).where(Offer.order_id == order.id, Offer.status == OfferStatus.sent)
    )
    for offer in pending.scalars():
        offer.status = OfferStatus.closed


async def fill_slots(session: AsyncSession, order: Order, now: datetime | None = None) -> list[Offer]:
    """Раздаёт свободные слоты следующим кандидатам. Возвращает новые предложения."""
    now = now or utcnow()
    if order.status != OrderStatus.open or not order.matching_active:
        return []
    if order.selection_deadline and now >= order.selection_deadline:
        await stop_matching(session, order)
        return []
    if await response_count(session, order.id) >= settings.order_response_limit:
        await stop_matching(session, order)
        return []

    active = (
        await session.execute(
            select(Offer.is_newbie, func.count())
            .where(Offer.order_id == order.id, Offer.status.in_(SLOT_STATUSES))
            .group_by(Offer.is_newbie)
        )
    ).all()
    active_by_group = {is_newbie: n for is_newbie, n in active}
    active_new = active_by_group.get(True, 0)
    active_exp = active_by_group.get(False, 0)
    free = settings.match_slots - active_new - active_exp
    if free <= 0:
        return []

    experienced, newbies = [], []
    for user in await _candidates(session, order, now):
        (newbies if user.is_newbie else experienced).append(Candidate(user, score(user, now)))

    offers = []
    for cand in pick(experienced, newbies, active_exp, active_new, free):
        offer = Offer(
            order_id=order.id,
            freelancer_id=cand.user.id,
            is_newbie=cand.user.is_newbie,
            score=round(cand.score, 4),
            sent_at=now,
            expires_at=now + timedelta(hours=settings.offer_ttl_hours),
        )
        session.add(offer)
        cand.user.offers_received += 1
        notify(
            session,
            cand.user,
            Kind.new_offer,
            f"Вам предложен заказ «{order.title}» за {order.price:.0f} ₽. "
            f"Ответьте в течение {settings.offer_ttl_hours} ч.",
            "/freelancer/orders",
        )
        offers.append(offer)
    await session.flush()
    return offers
