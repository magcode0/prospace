import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import func, or_, select

from app.models import (
    AdminAction,
    Category,
    ChatMessage,
    Dispute,
    DisputeStatus,
    Offer,
    Order,
    OrderStatus,
    Role,
    SupportTicket,
    TicketStatus,
    Transaction,
    User,
    utcnow,
)
from app.routers.balance import MONTHS_SHORT, TX_LABELS
from app.services import admin, stats
from app.services import orders as svc
from app.services.notifications import commit_and_push
from app.web import TZ, Session, StaffUser, flash, redirect, render
from app.worker.celery_app import schedule_expiry

router = APIRouter(prefix="/staff")

PAGE = 50


def _revenue_chart(rows: list[tuple], months: int = 6) -> dict:
    """Доход биржи по месяцам для HTML-графика."""
    df = pd.DataFrame(rows, columns=["created_at", "amount"])
    end = pd.Timestamp.now(tz=TZ).tz_localize(None).to_period("M")
    index = pd.period_range(end=end, periods=months, freq="M")
    if df.empty:
        series = pd.Series(0.0, index=index)
    else:
        df["amount"] = df["amount"].astype(float)
        df["month"] = pd.to_datetime(df["created_at"], utc=True).dt.tz_convert(TZ).dt.tz_localize(None).dt.to_period("M")
        series = df.groupby("month")["amount"].sum().reindex(index, fill_value=0.0)
    step = stats.nice_step(series.max())
    top = step * 4
    return {
        "ticks": [stats.short_money(step * i) for i in range(4, -1, -1)],
        "months": [{"label": MONTHS_SHORT[p.month - 1], "value": v, "h": round(v / top * 100, 1)} for p, v in series.items()],
    }


# ---------- сводка ----------

@router.get("")
async def dashboard(request: Request, session: Session, user: StaffUser):
    now = utcnow()
    week_ago = now - timedelta(days=7)
    month_start = now.astimezone(TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    platform_rows = (await session.execute(select(Transaction.created_at, Transaction.amount).where(Transaction.user_id.is_(None)))).all()
    revenue_total = sum(float(a) for _, a in platform_rows)
    revenue_month = sum(float(a) for t, a in platform_rows if t >= month_start)

    status_rows = (await session.execute(select(Order.status, func.count()).group_by(Order.status))).all()
    statuses = sorted(((stats.STATUS_LABELS[s.value], s.value, n) for s, n in status_rows), key=lambda x: -x[2])
    status_max = max((n for *_, n in statuses), default=1)

    c = {
        "users": await session.scalar(select(func.count()).select_from(User)),
        "users_week": await session.scalar(select(func.count()).where(User.created_at >= week_ago)),
        "freelancers": await session.scalar(select(func.count()).where(User.role == Role.freelancer)),
        "online": await session.scalar(select(func.count()).where(User.last_seen_at >= now - timedelta(minutes=15))),
        "moderation": await session.scalar(select(func.count()).where(Order.status == OrderStatus.moderation)),
        "disputes": await session.scalar(select(func.count()).where(Dispute.status == DisputeStatus.open)),
        "disputes_overdue": await session.scalar(select(func.count()).where(Dispute.status == DisputeStatus.open, Dispute.due_at <= now)),
        "tickets": await session.scalar(select(func.count()).where(SupportTicket.status == TicketStatus.open)),
        "escrow": await session.scalar(select(func.coalesce(func.sum(Order.escrow), 0))),
        "orders_active": await session.scalar(select(func.count()).where(Order.status.in_((OrderStatus.open, OrderStatus.in_progress, OrderStatus.submitted, OrderStatus.revision)))),
    }
    recent_orders = (await session.execute(select(Order).order_by(Order.created_at.desc()).limit(6))).scalars().all()
    recent_users = (await session.execute(select(User).order_by(User.created_at.desc()).limit(6))).scalars().all()
    return await render(
        request, session, "staff/dashboard.html", user,
        c=c, revenue_total=revenue_total, revenue_month=revenue_month, chart=_revenue_chart(platform_rows),
        statuses=statuses, status_max=status_max, recent_orders=recent_orders, recent_users=recent_users,
    )


@router.get("/report/revenue.png")
async def revenue_report(session: Session, user: StaffUser):
    rows = (await session.execute(select(Transaction.created_at, Transaction.amount).where(Transaction.user_id.is_(None)))).all()
    png = await run_in_threadpool(stats.bar_chart_monthly, stats.monthly(rows), "Доход биржи по месяцам")
    return Response(png, media_type="image/png", headers={"Content-Disposition": 'attachment; filename="prospace-revenue.png"'})


@router.get("/report/statuses.png")
async def statuses_report(session: Session, user: StaffUser):
    rows = (await session.execute(select(Order.status, func.count()).group_by(Order.status))).all()
    png = await run_in_threadpool(stats.status_breakdown, {s.value: n for s, n in rows}, "Заказы по статусам")
    return Response(png, media_type="image/png", headers={"Content-Disposition": 'attachment; filename="prospace-orders.png"'})


# ---------- пользователи ----------

@router.get("/users")
async def users(request: Request, session: Session, user: StaffUser, q: str = "", role: str = "", state: str = ""):
    stmt = select(User)
    if q.strip():
        like = f"%{q.strip()}%"
        conds = [User.username.ilike(like), User.email.ilike(like), User.first_name.ilike(like), User.last_name.ilike(like)]
        if q.strip().isdigit():
            conds.append(User.id == int(q.strip()))
        stmt = stmt.where(or_(*conds))
    if role in ("client", "freelancer"):
        stmt = stmt.where(User.role == Role(role))
    if state == "blocked":
        stmt = stmt.where(User.is_blocked.is_(True))
    elif state == "staff":
        stmt = stmt.where(User.is_staff.is_(True))
    elif state == "unverified":
        stmt = stmt.where(User.is_email_verified.is_(False))
    rows = (await session.execute(stmt.order_by(User.created_at.desc()).limit(PAGE))).scalars().all()
    return await render(request, session, "staff/users.html", user, users=rows, q=q, role=role, state=state)


async def _user(session, user_id: int) -> User:
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    return target


@router.get("/users/{user_id}")
async def user_detail(request: Request, session: Session, user: StaffUser, user_id: int):
    target = await _user(session, user_id)
    txs = (await session.execute(select(Transaction).where(Transaction.user_id == target.id).order_by(Transaction.created_at.desc()).limit(30))).scalars().all()
    orders_ = (
        await session.execute(
            select(Order).where(or_(Order.client_id == target.id, Order.freelancer_id == target.id)).order_by(Order.created_at.desc()).limit(20)
        )
    ).scalars().all()
    actions = (await session.execute(select(AdminAction).where(AdminAction.target == f"user:{target.id}").order_by(AdminAction.created_at.desc()).limit(20))).scalars().all()
    return await render(request, session, "staff/user_detail.html", user, target=target, txs=txs, orders=orders_, actions=actions, labels=TX_LABELS, action_labels=ACTION_LABELS)


async def _admin_act(request, session, fn, ok: str, back: str):
    try:
        result = fn()
        if hasattr(result, "__await__"):
            await result
    except admin.AdminError as exc:
        await session.rollback()
        flash(request, str(exc), "error")
        return redirect(back)
    await commit_and_push(session)
    flash(request, ok, "success")
    return redirect(back)


@router.post("/users/{user_id}/block")
async def block(request: Request, session: Session, user: StaffUser, user_id: int, reason: Annotated[str, Form()] = ""):
    target = await _user(session, user_id)
    return await _admin_act(request, session, lambda: admin.set_blocked(session, user, target, True, reason), "Пользователь заблокирован", f"/staff/users/{user_id}")


@router.post("/users/{user_id}/unblock")
async def unblock(request: Request, session: Session, user: StaffUser, user_id: int):
    target = await _user(session, user_id)
    return await _admin_act(request, session, lambda: admin.set_blocked(session, user, target, False), "Пользователь разблокирован", f"/staff/users/{user_id}")


@router.post("/users/{user_id}/staff")
async def toggle_staff(request: Request, session: Session, user: StaffUser, user_id: int):
    target = await _user(session, user_id)
    value = not target.is_staff
    return await _admin_act(request, session, lambda: admin.set_staff(session, user, target, value),
                            "Права сотрудника выданы" if value else "Права сотрудника сняты", f"/staff/users/{user_id}")


@router.post("/users/{user_id}/balance")
async def adjust(request: Request, session: Session, user: StaffUser, user_id: int,
                 amount: Annotated[str, Form()] = "", direction: Annotated[str, Form()] = "credit", reason: Annotated[str, Form()] = ""):
    await _user(session, user_id)
    try:
        value = Decimal(amount.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        flash(request, "Некорректная сумма", "error")
        return redirect(f"/staff/users/{user_id}")
    if not (Decimal("0") < value <= Decimal("1000000")):
        flash(request, "Сумма — от 0,01 до 1 000 000 ₽", "error")
        return redirect(f"/staff/users/{user_id}")
    signed = value if direction == "credit" else -value
    return await _admin_act(request, session, lambda: admin.adjust_balance(session, user, user_id, signed, reason),
                            "Баланс скорректирован", f"/staff/users/{user_id}")


# ---------- заказы ----------

ORDER_FILTERS = {
    "": "Все",
    "active": "Активные",
    "moderation": "Модерация",
    "problem": "Споры и отказы",
    "done": "Завершённые",
}
_FILTER_STATUSES = {
    "active": (OrderStatus.open, OrderStatus.in_progress, OrderStatus.submitted, OrderStatus.revision),
    "moderation": (OrderStatus.moderation,),
    "problem": (OrderStatus.declined, OrderStatus.disputed, OrderStatus.rejected),
    "done": (OrderStatus.completed, OrderStatus.cancelled),
}


@router.get("/orders")
async def orders_list(request: Request, session: Session, user: StaffUser, q: str = "", f: str = ""):
    stmt = select(Order)
    if f in _FILTER_STATUSES:
        stmt = stmt.where(Order.status.in_(_FILTER_STATUSES[f]))
    if q.strip():
        stmt = stmt.where(Order.id == int(q)) if q.strip().isdigit() else stmt.where(Order.title.ilike(f"%{q.strip()}%"))
    rows = (await session.execute(stmt.order_by(Order.created_at.desc()).limit(PAGE))).scalars().all()
    return await render(request, session, "staff/orders.html", user, orders=rows, q=q, f=f, filters=ORDER_FILTERS)


@router.get("/orders/{order_id}")
async def order_detail(request: Request, session: Session, user: StaffUser, order_id: int):
    order = await session.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    offers = (await session.execute(select(Offer).where(Offer.order_id == order.id).order_by(Offer.sent_at))).scalars().all()
    txs = (await session.execute(select(Transaction).where(Transaction.order_id == order.id).order_by(Transaction.created_at))).scalars().all()
    dispute = await session.scalar(select(Dispute).where(Dispute.order_id == order.id))
    threads = (await session.execute(select(ChatMessage.freelancer_id, func.count()).where(ChatMessage.order_id == order.id).group_by(ChatMessage.freelancer_id))).all()
    return await render(request, session, "staff/order_detail.html", user, order=order, offers=offers, txs=txs, dispute=dispute, threads=threads, labels=TX_LABELS)


@router.post("/orders/{order_id}/cancel")
async def order_cancel(request: Request, session: Session, user: StaffUser, order_id: int, reason: Annotated[str, Form()] = ""):
    order = await session.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    return await _admin_act(request, session, lambda: admin.force_cancel(session, user, order, reason), "Заказ снят, средства возвращены заказчику", f"/staff/orders/{order_id}")


# ---------- модерация, споры, обращения ----------

@router.get("/moderation")
async def moderation_queue(request: Request, session: Session, user: StaffUser):
    rows = (await session.execute(select(Order).where(Order.status == OrderStatus.moderation).order_by(Order.created_at))).scalars().all()
    return await render(request, session, "staff/moderation.html", user, orders=rows)


@router.post("/moderation/{order_id}")
async def moderate(request: Request, session: Session, user: StaffUser, order_id: int, decision: Annotated[str, Form()] = "", reason: Annotated[str, Form()] = ""):
    order = await session.get(Order, order_id)
    if not order or order.status != OrderStatus.moderation:
        raise HTTPException(404, "Заказ не найден или уже проверен")
    offers = []
    if decision == "approve":
        offers = await svc.publish_order(session, order)
        admin.log(session, user, "moderation_ok", f"order:{order.id}")
    else:
        why = reason.strip() or "Заказ не соответствует правилам биржи."
        await svc.reject_moderation(session, order, why)
        admin.log(session, user, "moderation_reject", f"order:{order.id}", why)
    await commit_and_push(session)
    schedule_expiry(offers)
    flash(request, f"Заказ #{order.id}: {'опубликован' if decision == 'approve' else 'отклонён'}", "success")
    return redirect("/staff/moderation")


@router.get("/disputes")
async def disputes(request: Request, session: Session, user: StaffUser):
    rows = (await session.execute(select(Dispute).order_by(Dispute.status, Dispute.due_at))).scalars().all()
    return await render(request, session, "staff/disputes.html", user, disputes=rows, now=utcnow())


@router.post("/disputes/{dispute_id}")
async def resolve(request: Request, session: Session, user: StaffUser, dispute_id: int, winner: Annotated[str, Form()] = "", resolution: Annotated[str, Form()] = ""):
    dispute = await session.get(Dispute, dispute_id)
    if not dispute:
        raise HTTPException(404, "Спор не найден")
    try:
        await svc.resolve_dispute(session, dispute, winner == "freelancer", resolution)
    except svc.DomainError as exc:
        await session.rollback()
        flash(request, str(exc), "error")
        return redirect("/staff/disputes")
    admin.log(session, user, "dispute", f"order:{dispute.order_id}", f"в пользу {'исполнителя' if winner == 'freelancer' else 'заказчика'}: {resolution.strip()}")
    await commit_and_push(session)
    flash(request, f"Спор #{dispute_id} закрыт", "success")
    return redirect("/staff/disputes")


@router.get("/tickets")
async def tickets(request: Request, session: Session, user: StaffUser):
    rows = (await session.execute(select(SupportTicket).order_by(SupportTicket.status, SupportTicket.created_at.desc()))).scalars().all()
    return await render(request, session, "staff/tickets.html", user, tickets=rows)


# ---------- категории ----------

SLUG_RE = re.compile(r"^[a-z0-9-]{2,64}$")


@router.get("/categories")
async def categories(request: Request, session: Session, user: StaffUser):
    rows = (
        await session.execute(
            select(Category, func.count(Order.id)).outerjoin(Order, Order.category_id == Category.id).group_by(Category.id).order_by(Category.name)
        )
    ).all()
    return await render(request, session, "staff/categories.html", user, categories=rows)


@router.post("/categories")
async def category_add(request: Request, session: Session, user: StaffUser, slug: Annotated[str, Form()] = "", name: Annotated[str, Form()] = ""):
    if not SLUG_RE.match(slug.strip().lower()) or len(name.strip()) < 2:
        flash(request, "Код — латиница, цифры и дефис; название — от 2 символов", "error")
        return redirect("/staff/categories")
    if await session.scalar(select(Category.id).where(Category.slug == slug.strip().lower())):
        flash(request, "Категория с таким кодом уже есть", "error")
        return redirect("/staff/categories")
    admin.add_category(session, user, slug, name)
    await session.commit()
    flash(request, "Категория добавлена", "success")
    return redirect("/staff/categories")


@router.post("/categories/{cat_id}/rename")
async def category_rename(request: Request, session: Session, user: StaffUser, cat_id: int, name: Annotated[str, Form()] = ""):
    cat = await session.get(Category, cat_id)
    if not cat or len(name.strip()) < 2:
        flash(request, "Укажите название", "error")
        return redirect("/staff/categories")
    admin.log(session, user, "category_rename", f"category:{cat.slug}", f"{cat.name} → {name.strip()}")
    cat.name = name.strip()[:128]
    await session.commit()
    flash(request, "Категория переименована", "success")
    return redirect("/staff/categories")


@router.post("/categories/{cat_id}/delete")
async def category_delete(request: Request, session: Session, user: StaffUser, cat_id: int):
    cat = await session.get(Category, cat_id)
    if not cat:
        raise HTTPException(404, "Категория не найдена")
    if await session.scalar(select(func.count()).where(Order.category_id == cat.id)):
        flash(request, "В категории есть заказы — её можно только переименовать", "error")
        return redirect("/staff/categories")
    admin.log(session, user, "category_delete", f"category:{cat.slug}", cat.name)
    await session.delete(cat)
    await session.commit()
    flash(request, "Категория удалена", "success")
    return redirect("/staff/categories")


# ---------- журнал ----------

ACTION_LABELS = {
    "balance": "Корректировка баланса", "block": "Блокировка", "unblock": "Разблокировка",
    "staff_on": "Выданы права сотрудника", "staff_off": "Сняты права сотрудника", "order_cancel": "Заказ снят поддержкой",
    "moderation_ok": "Заказ одобрен", "moderation_reject": "Заказ отклонён модерацией", "dispute": "Спор решён",
    "category_add": "Категория добавлена", "category_rename": "Категория переименована", "category_delete": "Категория удалена",
}


@router.get("/log")
async def action_log(request: Request, session: Session, user: StaffUser):
    rows = (await session.execute(select(AdminAction).order_by(AdminAction.created_at.desc()).limit(200))).scalars().all()
    return await render(request, session, "staff/log.html", user, actions=rows, action_labels=ACTION_LABELS)
