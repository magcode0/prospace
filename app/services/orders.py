"""Жизненный цикл заказа, отклики и безопасная сделка.

Функции меняют объекты в сессии; коммит и рассылку делает вызывающий код через commit_and_push().
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ACTIVE_WORK_STATUSES,
    ChatMessage,
    Dispute,
    DisputeStatus,
    Offer,
    OfferStatus,
    Order,
    OrderStatus,
    Review,
    TxKind,
    User,
    utcnow,
)
from app.services import billing, matching
from app.services.notifications import Kind, notify


class DomainError(Exception):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DomainError(message)


# ---------- суточный лимит откликов ----------

def refresh_credits(user: User, today: date | None = None) -> None:
    """5 откликов в сутки; неиспользованные копятся максимум за 2 суток."""
    today = today or date.today()
    cap = settings.daily_responses * settings.responses_carry_days
    days = (today - user.credits_refreshed_on).days
    if days > 0:
        user.response_credits = min(user.response_credits + settings.daily_responses * days, cap)
        user.credits_refreshed_on = today


def return_credit(user: User) -> None:
    cap = settings.daily_responses * settings.responses_carry_days
    user.response_credits = min(user.response_credits + 1, cap)


# ---------- заказчик: размещение и снятие ----------

async def create_order(
    session: AsyncSession,
    client: User,
    *,
    title: str,
    description: str,
    skills: str,
    category_id: int,
    price: Decimal,
    deadline: datetime,
) -> Order:
    _require(price >= settings.min_order_price, f"Минимальная сумма заказа — {settings.min_order_price:.0f} ₽")
    _require(deadline > utcnow() + timedelta(hours=settings.deadline_reminder_hours), "Срок должен быть позже, чем через 5 часов")

    client = await billing.lock_user(session, client.id)
    total = settings.placement_fee + price
    if client.balance < total:
        raise DomainError(
            f"Для размещения нужно {total:.0f} ₽ (сумма заказа + {settings.placement_fee:.0f} ₽ за размещение). "
            f"На балансе {client.balance:.0f} ₽."
        )
    order = Order(
        client_id=client.id,
        title=title.strip(),
        description=description.strip(),
        skills=skills.strip(),
        category_id=category_id,
        price=billing.money(price),
        deadline=deadline,
        status=OrderStatus.moderation,
    )
    session.add(order)
    await session.flush()
    await billing.charge(session, client, settings.placement_fee, TxKind.placement_fee, order.id)
    await billing.charge(session, client, price, TxKind.escrow_hold, order.id)
    order.placement_fee = settings.placement_fee
    order.escrow = billing.money(price)
    return order


async def publish_order(session: AsyncSession, order: Order) -> list[Offer]:
    """После одобрения модерацией: заказ открыт, запускается подбор."""
    now = utcnow()
    order.status = OrderStatus.open
    order.published_at = now
    order.selection_deadline = min(now + timedelta(hours=settings.selection_deadline_hours), order.deadline)
    order.matching_active = True
    notify(session, order.client, Kind.moderation, f"Заказ «{order.title}» опубликован, идёт подбор исполнителей.", f"/client/orders/{order.id}")
    return await matching.fill_slots(session, order, now)


async def _refund_open_responses(session: AsyncSession, order: Order, except_offer_id: int | None = None) -> None:
    """Вернуть 50 ₽ всем откликнувшимся, кого не выбрали."""
    offers = await session.execute(
        select(Offer).where(Offer.order_id == order.id, Offer.status == OfferStatus.responded)
    )
    for offer in offers.scalars():
        if offer.id == except_offer_id:
            continue
        offer.status = OfferStatus.closed
        if offer.fee_paid > 0:
            freelancer = await billing.lock_user(session, offer.freelancer_id)
            await billing.credit(session, freelancer, offer.fee_paid, TxKind.response_refund, order.id)
            offer.fee_paid = Decimal("0")


async def reject_moderation(session: AsyncSession, order: Order, reason: str) -> None:
    order.status = OrderStatus.rejected
    order.moderation_note = reason
    await _refund_everything(session, order)
    notify(session, order.client, Kind.moderation, f"Заказ «{order.title}» не прошёл модерацию: {reason} Средства возвращены.", f"/client/orders/{order.id}")


async def _refund_everything(session: AsyncSession, order: Order) -> None:
    client = await billing.lock_user(session, order.client_id)
    await billing.credit(session, client, order.placement_fee, TxKind.placement_refund, order.id)
    await billing.credit(session, client, order.escrow, TxKind.escrow_refund, order.id)
    order.placement_fee = Decimal("0")
    order.escrow = Decimal("0")
    order.closed_at = utcnow()


async def withdraw_order(session: AsyncSession, order: Order, client: User) -> None:
    """Снятие заказа до назначения исполнителя: возвращаются сумма заказа и 100 ₽ за размещение."""
    _require(order.client_id == client.id, "Это не ваш заказ")
    _require(
        order.status in (OrderStatus.moderation, OrderStatus.open),
        "Снять можно только заказ без исполнителя. Назначенный заказ можно отклонить.",
    )
    await matching.stop_matching(session, order)
    await _refund_open_responses(session, order)
    order.status = OrderStatus.cancelled
    await _refund_everything(session, order)


# ---------- исполнитель: отклик / отказ ----------

async def respond(session: AsyncSession, offer: Offer, freelancer: User, message: str) -> None:
    _require(offer.freelancer_id == freelancer.id, "Это предложение адресовано не вам")
    _require(offer.status == OfferStatus.sent and offer.expires_at > utcnow(), "Предложение больше не активно")
    order = offer.order
    _require(order.status == OrderStatus.open and order.matching_active, "Подбор по заказу завершён")
    _require(len(message.strip()) >= 10, "Напишите заказчику пару слов о себе и задаче (от 10 символов)")

    freelancer = await billing.lock_user(session, freelancer.id)
    refresh_credits(freelancer)
    _require(freelancer.response_credits > 0, "Суточный лимит откликов исчерпан")
    try:
        await billing.charge(session, freelancer, settings.response_fee, TxKind.response_fee, order.id)
    except billing.InsufficientFunds:
        raise DomainError(f"Отклик стоит {settings.response_fee:.0f} ₽ — пополните баланс") from None

    freelancer.response_credits -= 1
    freelancer.offers_reacted += 1
    offer.status = OfferStatus.responded
    offer.message = message.strip()
    offer.fee_paid = settings.response_fee
    offer.responded_at = utcnow()
    notify(session, order.client, Kind.new_response, f"{freelancer.full_name} откликнулся на заказ «{order.title}».", f"/client/orders/{order.id}")
    await session.flush()
    if await matching.response_count(session, order.id) >= settings.order_response_limit:
        await matching.stop_matching(session, order)


async def decline_offer(session: AsyncSession, offer: Offer, freelancer: User) -> None:
    _require(offer.freelancer_id == freelancer.id, "Это предложение адресовано не вам")
    _require(offer.status == OfferStatus.sent, "Предложение уже неактивно")
    offer.status = OfferStatus.declined
    freelancer.offers_reacted += 1
    await matching.fill_slots(session, offer.order)


async def expire_offer(session: AsyncSession, offer: Offer, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if offer.status != OfferStatus.sent or offer.expires_at > now:
        return False
    offer.status = OfferStatus.expired  # молчание = отказ, offers_reacted не растёт
    await matching.fill_slots(session, offer.order, now)
    return True


# ---------- заказчик: решение по откликам ----------

async def reject_response(session: AsyncSession, offer: Offer, client: User) -> None:
    order = offer.order
    _require(order.client_id == client.id, "Это не ваш заказ")
    _require(offer.status == OfferStatus.responded, "Отклик уже обработан")
    offer.status = OfferStatus.rejected
    freelancer = await billing.lock_user(session, offer.freelancer_id)
    await billing.credit(session, freelancer, offer.fee_paid, TxKind.response_refund, order.id)
    offer.fee_paid = Decimal("0")
    return_credit(freelancer)
    await matching.fill_slots(session, order)


async def accept_response(session: AsyncSession, offer: Offer, client: User) -> None:
    order = offer.order
    _require(order.client_id == client.id, "Это не ваш заказ")
    _require(order.status == OrderStatus.open, "Исполнитель уже выбран или заказ закрыт")
    _require(offer.status == OfferStatus.responded, "Можно выбрать только откликнувшегося исполнителя")

    offer.status = OfferStatus.accepted
    await matching.stop_matching(session, order)
    await _refund_open_responses(session, order, except_offer_id=offer.id)

    # плата за отклик выбранного исполнителя и плата за размещение остаются бирже
    billing.platform_income(session, offer.fee_paid, TxKind.response_fee, order.id)
    billing.platform_income(session, order.placement_fee, TxKind.placement_fee, order.id)
    order.placement_fee = Decimal("0")

    order.freelancer = offer.freelancer
    order.status = OrderStatus.in_progress
    order.assigned_at = utcnow()
    notify(session, offer.freelancer, Kind.selected, f"Вы выбраны исполнителем заказа «{order.title}». Срок: {order.deadline.astimezone(ZoneInfo(settings.timezone)):%d.%m.%Y %H:%M} (МСК).", f"/freelancer/work/{order.id}")


# ---------- работа ----------

async def submit_work(session: AsyncSession, order: Order, freelancer: User, text: str) -> None:
    _require(order.freelancer_id == freelancer.id, "Вы не исполнитель этого заказа")
    _require(order.status in (OrderStatus.in_progress, OrderStatus.revision), "Сейчас нельзя отправить работу")
    _require(len(text.strip()) >= 5, "Опишите результат или приложите ссылку")
    order.status = OrderStatus.submitted
    order.submitted_at = utcnow()
    session.add(ChatMessage(order_id=order.id, freelancer_id=freelancer.id, sender_id=freelancer.id, text=f"📦 Работа отправлена:\n{text.strip()}"))
    notify(session, order.client, Kind.work_submitted, f"Исполнитель отправил работу по заказу «{order.title}».", f"/client/orders/{order.id}")


async def accept_work(session: AsyncSession, order: Order, client: User) -> None:
    _require(order.client_id == client.id, "Это не ваш заказ")
    _require(order.status in (OrderStatus.submitted, OrderStatus.revision, OrderStatus.in_progress), "Сейчас нельзя принять работу")
    await _pay_freelancer(session, order)
    notify(session, order.freelancer, Kind.work_accepted, f"Работа по заказу «{order.title}» принята. Деньги зачислены на баланс.", f"/freelancer/work/{order.id}")


async def _pay_freelancer(session: AsyncSession, order: Order) -> None:
    payout, commission = billing.split_payout(order.escrow)
    freelancer = await billing.lock_user(session, order.freelancer_id)
    await billing.credit(session, freelancer, payout, TxKind.payout, order.id)
    billing.platform_income(session, commission, TxKind.commission, order.id)
    freelancer.completed_orders += 1
    order.escrow = Decimal("0")
    order.status = OrderStatus.completed
    order.closed_at = utcnow()


async def request_revision(session: AsyncSession, order: Order, client: User, comment: str) -> None:
    _require(order.client_id == client.id, "Это не ваш заказ")
    _require(order.status == OrderStatus.submitted, "Правки можно запросить только по отправленной работе")
    _require(order.deadline > utcnow(), "Срок заказа истёк")
    order.status = OrderStatus.revision
    if comment.strip():
        session.add(ChatMessage(order_id=order.id, freelancer_id=order.freelancer_id, sender_id=client.id, text=f"✏️ Правки:\n{comment.strip()}"))
    notify(session, order.freelancer, Kind.revision, f"По заказу «{order.title}» требуются правки.", f"/freelancer/work/{order.id}")


def open_dispute_window(order: Order, now: datetime | None = None) -> None:
    order.status = OrderStatus.declined
    order.dispute_window_until = (now or utcnow()) + timedelta(hours=settings.dispute_window_hours)


async def decline_work(session: AsyncSession, order: Order, client: User) -> None:
    """Заказчик отклоняет заказ: открывается окно спора 2 часа, деньги заморожены."""
    _require(order.client_id == client.id, "Это не ваш заказ")
    _require(order.status in ACTIVE_WORK_STATUSES, "Сейчас нельзя отклонить заказ")
    open_dispute_window(order)
    notify(
        session,
        order.freelancer,
        Kind.work_declined,
        f"Заказчик отклонил заказ «{order.title}». В течение {settings.dispute_window_hours} ч вы можете обжаловать решение в поддержке.",
        f"/freelancer/work/{order.id}",
    )


async def open_dispute(session: AsyncSession, order: Order, freelancer: User, reason: str) -> Dispute:
    _require(order.freelancer_id == freelancer.id, "Вы не исполнитель этого заказа")
    _require(
        order.status == OrderStatus.declined and order.dispute_window_until and utcnow() < order.dispute_window_until,
        "Окно для спора закрыто",
    )
    _require(len(reason.strip()) >= 20, "Опишите ситуацию подробнее (от 20 символов)")
    order.status = OrderStatus.disputed
    dispute = Dispute(
        order_id=order.id,
        freelancer_id=freelancer.id,
        reason=reason.strip(),
        due_at=utcnow() + timedelta(hours=settings.dispute_review_hours),
    )
    session.add(dispute)
    notify(session, order.client, Kind.dispute, f"Исполнитель обжаловал решение по заказу «{order.title}». Поддержка рассмотрит спор в течение 24 ч.", f"/client/orders/{order.id}")
    return dispute


async def resolve_dispute(session: AsyncSession, dispute: Dispute, in_favor_of_freelancer: bool, resolution: str) -> None:
    _require(dispute.status == DisputeStatus.open, "Спор уже закрыт")
    order = dispute.order
    dispute.resolution = resolution.strip()
    dispute.resolved_at = utcnow()
    if in_favor_of_freelancer:
        dispute.status = DisputeStatus.resolved_freelancer
        await _pay_freelancer(session, order)
        outcome = "в пользу исполнителя — оплата перечислена исполнителю"
    else:
        dispute.status = DisputeStatus.resolved_client
        await _return_escrow(session, order)
        outcome = "в пользу заказчика — сумма заказа возвращена заказчику"
    for user in (order.client, order.freelancer):
        notify(session, user, Kind.dispute, f"Спор по заказу «{order.title}» решён {outcome}. {resolution}", None)


async def _return_escrow(session: AsyncSession, order: Order) -> None:
    client = await billing.lock_user(session, order.client_id)
    await billing.credit(session, client, order.escrow, TxKind.escrow_refund, order.id)
    order.escrow = Decimal("0")
    order.status = OrderStatus.cancelled
    order.closed_at = utcnow()


async def close_dispute_window(session: AsyncSession, order: Order) -> None:
    """Окно спора истекло без обращения исполнителя — сумма заказа возвращается заказчику."""
    await _return_escrow(session, order)
    notify(session, order.client, Kind.finance, f"Сумма заказа «{order.title}» возвращена на баланс.", f"/client/orders/{order.id}")


# ---------- отзывы ----------

async def leave_review(session: AsyncSession, order: Order, author: User, score: int, text: str) -> None:
    _require(order.status == OrderStatus.completed, "Отзыв можно оставить после завершения заказа")
    _require(author.id in (order.client_id, order.freelancer_id), "Вы не участник заказа")
    _require(1 <= score <= 5, "Оценка от 1 до 5")
    exists = await session.scalar(select(Review.id).where(Review.order_id == order.id, Review.author_id == author.id))
    _require(exists is None, "Вы уже оставили отзыв")
    target_id = order.freelancer_id if author.id == order.client_id else order.client_id
    session.add(Review(order_id=order.id, author_id=author.id, target_id=target_id, score=score, text=text.strip()[:1000]))
    target = await billing.lock_user(session, target_id)
    target.rating = round((target.rating * target.rating_count + score) / (target.rating_count + 1), 2)
    target.rating_count += 1
