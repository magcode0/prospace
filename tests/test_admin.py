"""Админка: блокировка, корректировка баланса, снятие заказа, журнал, страницы панели."""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.models import AdminAction, Category, OrderStatus, Role, Transaction, TxKind, User, utcnow
from app.security import hash_password
from app.services import admin
from app.services import orders as svc
from app.services.notifications import commit_and_push

_seq = 0
PW = "Admin#2026x"


async def make_user(session, role=Role.client, **kw) -> User:
    global _seq
    _seq += 1
    user = User(
        first_name="Адм", last_name=f"Тест{_seq}", email=f"adm{_seq}@t.local", username=f"adm{_seq}",
        password_hash=hash_password(PW), role=role, is_email_verified=True, **kw,
    )
    session.add(user)
    await session.flush()
    return user


async def test_adjust_balance_and_log(session):
    staff = await make_user(session, is_staff=True)
    target = await make_user(session, balance=Decimal("100"))
    await session.commit()
    sid, tid = staff.id, target.id  # после rollback объекты «протухают»

    await admin.adjust_balance(session, staff, target.id, Decimal("250"), "компенсация")
    await commit_and_push(session)
    assert target.balance == Decimal("350.00")

    with pytest.raises(admin.AdminError):
        await admin.adjust_balance(session, staff, tid, Decimal("-1000"), "списание")
    await session.rollback()
    staff = await session.get(User, sid)
    with pytest.raises(admin.AdminError):
        await admin.adjust_balance(session, staff, tid, Decimal("10"), "  ")
    await session.rollback()

    kinds = (await session.execute(select(Transaction.kind).where(Transaction.user_id == tid))).scalars().all()
    assert kinds == [TxKind.adjustment]
    log = (await session.execute(select(AdminAction).where(AdminAction.target == f"user:{tid}"))).scalars().all()
    assert [a.action for a in log] == ["balance"]


async def test_cannot_block_or_unstaff_self(session):
    staff = await make_user(session, is_staff=True)
    with pytest.raises(admin.AdminError):
        admin.set_blocked(session, staff, staff, True)
    with pytest.raises(admin.AdminError):
        admin.set_staff(session, staff, staff, False)


async def test_force_cancel_refunds_client(session):
    staff = await make_user(session, is_staff=True)
    client = await make_user(session, balance=Decimal("2000"))
    cat = Category(slug=f"adm-cat-{_seq}", name="Тест")
    session.add(cat)
    await session.flush()
    order = await svc.create_order(
        session, client, title="Сомнительный заказ", description="Описание достаточно длинное для модерации",
        skills="", category_id=cat.id, price=Decimal("1500"), deadline=utcnow() + timedelta(days=2),
    )
    await commit_and_push(session)
    assert client.balance == Decimal("400.00")

    await admin.force_cancel(session, staff, order, "нарушение правил")
    await commit_and_push(session)
    await session.refresh(client)
    assert order.status == OrderStatus.cancelled
    assert client.balance == Decimal("2000.00")


async def test_blocked_user_cannot_login_and_staff_pages_render(session):
    staff = await make_user(session, is_staff=True)
    victim = await make_user(session)
    await session.commit()

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as c:
        await c.post("/login", data={"login": staff.username, "password": PW})
        for path in ["/staff", "/staff/users", "/staff/users?state=staff&q=adm", f"/staff/users/{victim.id}",
                     "/staff/orders", "/staff/orders?f=active", "/staff/moderation", "/staff/disputes",
                     "/staff/tickets", "/staff/categories", "/staff/log"]:
            r = await c.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[-400:])

        r = await c.post(f"/staff/users/{victim.id}/block", data={"reason": "спам"})
        assert r.status_code == 200 and "заблокирован" in r.text

    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as c:
        r = await c.post("/login", data={"login": victim.username, "password": PW})
        assert r.status_code == 403 and "заблокирован" in r.text
