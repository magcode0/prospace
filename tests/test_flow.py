"""Интеграционный прогон: подбор → отклики → безопасная сделка → споры. Нужны Postgres и Redis."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import (
    Category,
    Offer,
    OfferStatus,
    OrderStatus,
    Role,
    Transaction,
    User,
    utcnow,
)
from app.services import orders as svc
from app.services import scheduler
from app.services.notifications import commit_and_push

pytestmark = pytest.mark.asyncio(loop_scope="function")

_seq = 0


async def make_category(session) -> Category:
    """Своя категория на тест — исполнители разных тестов не пересекаются в подборе."""
    global _seq
    _seq += 1
    cat = Category(slug=f"cat{_seq}", name=f"Категория {_seq}")
    session.add(cat)
    await session.flush()
    return cat


async def make_user(session, role: Role, cat: Category | None = None, *, balance="0", completed=0, **kw) -> User:
    global _seq
    _seq += 1
    user = User(
        first_name="Тест", last_name=f"Пользователь{_seq}", email=f"u{_seq}@t.local", username=f"user{_seq}",
        password_hash="x", role=role, is_email_verified=True, balance=Decimal(balance),
        completed_orders=completed, work_hours_start=0, work_hours_end=0,
        categories=[cat] if cat else [], **kw,
    )
    session.add(user)
    await session.flush()
    return user


async def platform_total(session) -> Decimal:
    return await session.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(Transaction.user_id.is_(None)))


async def new_published_order(session, client, cat, price="5000"):
    order = await svc.create_order(
        session, client, title="Сделать API", description="Нужен REST API на FastAPI с PostgreSQL и тестами",
        skills="Python", category_id=cat.id, price=Decimal(price), deadline=utcnow() + timedelta(days=3),
    )
    await session.flush()
    await svc.publish_order(session, order)
    await commit_and_push(session)
    return order


async def slots(session, order):
    rows = (await session.execute(select(Offer).where(Offer.order_id == order.id))).scalars().all()
    return {o.freelancer_id: o for o in rows}


async def test_full_safe_deal(session):
    cat = await make_category(session)
    client = await make_user(session, Role.client, balance="10000")
    pros = [await make_user(session, Role.freelancer, cat, balance="1000", completed=10 + i, rating=4.5) for i in range(9)]
    newbies = [await make_user(session, Role.freelancer, cat, balance="1000") for _ in range(4)]
    # не подходят: занят / давно не в сети / минималка выше цены
    await make_user(session, Role.freelancer, cat, completed=50, busy_status="busy")
    await make_user(session, Role.freelancer, cat, completed=50, last_seen_at=utcnow() - timedelta(hours=5))
    await make_user(session, Role.freelancer, cat, completed=50, min_price=Decimal("9000"))
    await session.commit()

    order = await new_published_order(session, client, cat)
    assert client.balance == Decimal("4900.00")  # 10000 − 5000 − 100
    offers = await slots(session, order)
    assert len(offers) == 10
    assert sum(o.is_newbie for o in offers.values()) == 3

    # отказ освобождает слот → его получает следующий опытный
    first = next(o for o in offers.values() if not o.is_newbie)
    await svc.decline_offer(session, first, first.freelancer)
    await commit_and_push(session)
    offers = await slots(session, order)
    assert len(offers) == 11
    assert offers[first.freelancer_id].status == OfferStatus.declined

    active = [o for o in offers.values() if o.status == OfferStatus.sent]
    a, b = active[0], active[1]
    await svc.respond(session, a, a.freelancer, "Сделаю за 2 дня, опыт есть")
    await svc.respond(session, b, b.freelancer, "Готов взяться, есть похожие проекты")
    await commit_and_push(session)
    assert a.freelancer.balance == Decimal("950.00")
    assert a.freelancer.response_credits == 4

    # отказ заказчика: 50 ₽ и отклик возвращаются
    await svc.reject_response(session, b, client)
    await commit_and_push(session)
    assert b.freelancer.balance == Decimal("1000.00")
    assert b.freelancer.response_credits == 5
    assert len(await slots(session, order)) == 12  # слот снова отдан

    await svc.accept_response(session, a, client)
    await commit_and_push(session)
    assert order.status == OrderStatus.in_progress and order.freelancer_id == a.freelancer_id
    assert all(o.status != OfferStatus.sent for o in (await slots(session, order)).values())
    assert await platform_total(session) == Decimal("150.00")  # размещение + отклик выбранного

    await svc.submit_work(session, order, a.freelancer, "Готово: https://git.example/api")
    await svc.request_revision(session, order, client, "Добавьте пагинацию")
    await svc.submit_work(session, order, a.freelancer, "Исправил")
    await svc.accept_work(session, order, client)
    await commit_and_push(session)
    assert order.status == OrderStatus.completed
    assert a.freelancer.balance == Decimal("5700.00")  # 950 + 4750
    assert await platform_total(session) == Decimal("400.00")  # + комиссия 250

    await svc.leave_review(session, order, client, 5, "Отлично")
    await commit_and_push(session)
    assert a.freelancer.rating_count == 1


async def test_withdraw_before_assignment_refunds_everything(session):
    cat = await make_category(session)
    client = await make_user(session, Role.client, balance="2000")
    fr = await make_user(session, Role.freelancer, cat, balance="100", completed=5)
    await session.commit()
    order = await new_published_order(session, client, cat, price="1500")
    offer = (await slots(session, order))[fr.id]
    await svc.respond(session, offer, fr, "Сделаю быстро и качественно")
    await svc.withdraw_order(session, order, client)
    await commit_and_push(session)
    assert client.balance == Decimal("2000.00")
    assert fr.balance == Decimal("100.00")


async def test_dispute_window_and_resolution(session):
    cat = await make_category(session)
    client = await make_user(session, Role.client, balance="3000")
    fr = await make_user(session, Role.freelancer, cat, balance="100", completed=5)
    await session.commit()
    order = await new_published_order(session, client, cat, price="2000")
    offer = (await slots(session, order))[fr.id]
    await svc.respond(session, offer, fr, "Беру, сделаю к сроку")
    await svc.accept_response(session, offer, client)
    await svc.decline_work(session, order, client)
    await commit_and_push(session)
    assert order.status == OrderStatus.declined

    with pytest.raises(svc.DomainError):
        await svc.withdraw_order(session, order, client)  # в окне спора снять нельзя

    dispute = await svc.open_dispute(session, order, fr, "Работа сделана полностью по ТЗ, есть переписка")
    await commit_and_push(session)
    await svc.resolve_dispute(session, dispute, True, "ТЗ выполнено")
    await commit_and_push(session)
    assert fr.balance == Decimal("1950.00")  # 100 − 50 + 1900


async def test_window_expires_without_dispute_returns_money(session):
    cat = await make_category(session)
    client = await make_user(session, Role.client, balance="3000")
    fr = await make_user(session, Role.freelancer, cat, balance="100", completed=5)
    await session.commit()
    order = await new_published_order(session, client, cat, price="2000")
    offer = (await slots(session, order))[fr.id]
    await svc.respond(session, offer, fr, "Беру, сделаю к сроку")
    await svc.accept_response(session, offer, client)
    await commit_and_push(session)
    assert client.balance == Decimal("900.00")

    # срок истёк → окно спора → через 2 ч деньги у заказчика
    await scheduler.tick(session, order.deadline + timedelta(minutes=1))
    assert order.status == OrderStatus.declined
    await scheduler.tick(session, order.deadline + timedelta(hours=2, minutes=2))
    await session.refresh(client)
    assert order.status == OrderStatus.cancelled
    assert client.balance == Decimal("2900.00")  # плата за размещение не возвращается


async def test_silence_expires_offer_and_frees_slot(session):
    cat = await make_category(session)
    client = await make_user(session, Role.client, balance="3000")
    fr = await make_user(session, Role.freelancer, cat, completed=5)
    await session.commit()
    order = await new_published_order(session, client, cat, price="2000")
    offer = (await slots(session, order))[fr.id]
    await scheduler.tick(session, offer.expires_at + timedelta(seconds=1))
    await session.refresh(offer)
    assert offer.status == OfferStatus.expired
