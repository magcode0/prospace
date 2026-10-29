from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import func, select

from app.models import (
    ACTIVE_WORK_STATUSES,
    Category,
    Dispute,
    Offer,
    OfferStatus,
    Order,
    OrderStatus,
    Review,
    Transaction,
    TxKind,
)
from app.services import orders as svc
from app.services.notifications import commit_and_push
from app.web import TZ, ClientUser, Session, flash, month_bars, redirect, render
from app.worker.celery_app import moderate_order

router = APIRouter(prefix="/client")

TABS = {
    "active": ("Ищем исполнителя", (OrderStatus.moderation, OrderStatus.open)),
    "work": ("Взяты исполнителем", (*ACTIVE_WORK_STATUSES, OrderStatus.declined, OrderStatus.disputed)),
    "history": ("История", (OrderStatus.completed, OrderStatus.cancelled, OrderStatus.rejected)),
}


async def _own_order(session, order_id: int, user) -> Order:
    order = await session.get(Order, order_id)
    if not order or order.client_id != user.id:
        raise HTTPException(404, "Заказ не найден")
    return order


@router.get("/orders")
async def my_orders(request: Request, session: Session, user: ClientUser, tab: str = "active"):
    tab = tab if tab in TABS else "active"
    rows = (
        await session.execute(
            select(Order).where(Order.client_id == user.id, Order.status.in_(TABS[tab][1])).order_by(Order.created_at.desc())
        )
    ).scalars().all()
    return await render(request, session, "client/orders.html", user, orders=rows, tab=tab, tabs=TABS, summary=await _summary(session, user))


async def _summary(session, user) -> dict:
    """Данные для бенто-плиток над списком заказов."""
    escrow = await session.scalar(
        select(func.coalesce(func.sum(Order.escrow), 0)).where(Order.client_id == user.id)
    )
    responders = (
        await session.execute(
            select(Offer)
            .join(Order, Order.id == Offer.order_id)
            .where(Order.client_id == user.id, Order.status == OrderStatus.open, Offer.status == OfferStatus.responded)
            .order_by(Offer.responded_at.desc())
        )
    ).scalars().all()
    waiting = (
        await session.execute(
            select(Order).where(Order.client_id == user.id, Order.status == OrderStatus.submitted).order_by(Order.deadline)
        )
    ).scalars().all()
    spend = (
        await session.execute(
            select(Transaction.created_at, Transaction.amount).where(
                Transaction.user_id == user.id, Transaction.kind.in_((TxKind.escrow_hold, TxKind.placement_fee))
            )
        )
    ).all()
    # доля зарезервированного от «баланс + резерв» — для кольца
    total = float(user.balance) + float(escrow)
    return {
        "escrow": escrow,
        "escrow_pct": round(float(escrow) / total * 100) if total else 0,
        "responders": responders,
        "waiting": waiting,
        "bars": month_bars(spend),
    }


@router.get("/orders/new")
async def new_order_page(request: Request, session: Session, user: ClientUser):
    categories = (await session.execute(select(Category).order_by(Category.name))).scalars().all()
    return await render(request, session, "client/order_new.html", user, categories=categories, form={}, error=None)


@router.post("/orders/new")
async def new_order(
    request: Request,
    session: Session,
    user: ClientUser,
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    skills: Annotated[str, Form()] = "",
    category_id: Annotated[int, Form()] = 0,
    price: Annotated[str, Form()] = "",
    deadline: Annotated[str, Form()] = "",
):
    form = {"title": title, "description": description, "skills": skills, "category_id": category_id, "price": price, "deadline": deadline}
    categories = (await session.execute(select(Category).order_by(Category.name))).scalars().all()
    error = None
    try:
        amount = Decimal(price.replace(" ", "").replace(",", "."))
        due = datetime.fromisoformat(deadline).replace(tzinfo=TZ)
    except (InvalidOperation, ValueError):
        amount, due = None, None
        error = "Проверьте сумму и срок"
    if not error and not (3 <= len(title.strip()) <= 150):
        error = "Название — от 3 до 150 символов"
    if not error and not any(c.id == category_id for c in categories):
        error = "Выберите категорию"
    if not error:
        try:
            order = await svc.create_order(
                session, user, title=title, description=description, skills=skills,
                category_id=category_id, price=amount, deadline=due,
            )
        except svc.DomainError as exc:
            # create_order проверяет всё до изменения данных, откатывать нечего
            error = str(exc)
    if error:
        return await render(request, session, "client/order_new.html", user, categories=categories, form=form, error=error, status_code=422)
    await commit_and_push(session)
    moderate_order.delay(order.id)
    flash(request, "Заказ отправлен на модерацию. После проверки начнётся подбор исполнителей.", "success")
    return redirect(f"/client/orders/{order.id}")


@router.get("/orders/{order_id}")
async def order_detail(request: Request, session: Session, user: ClientUser, order_id: int):
    order = await _own_order(session, order_id, user)
    responses = (
        await session.execute(
            select(Offer)
            .where(Offer.order_id == order.id, Offer.status.in_((OfferStatus.responded, OfferStatus.accepted)))
            .order_by(Offer.responded_at)
        )
    ).scalars().all()
    offers_out = (
        await session.execute(select(Offer).where(Offer.order_id == order.id, Offer.status == OfferStatus.sent))
    ).scalars().all()
    dispute = await session.scalar(select(Dispute).where(Dispute.order_id == order.id))
    reviewed = await session.scalar(select(Review.id).where(Review.order_id == order.id, Review.author_id == user.id))
    return await render(
        request, session, "client/order_detail.html", user,
        order=order, responses=responses, pending_offers=len(offers_out), dispute=dispute, reviewed=reviewed,
    )


async def _act(request: Request, session, coro, ok: str, back: str):
    try:
        await coro
    except svc.DomainError as exc:
        await session.rollback()
        flash(request, str(exc), "error")
        return redirect(back)
    await commit_and_push(session)
    flash(request, ok, "success")
    return redirect(back)


@router.post("/orders/{order_id}/withdraw")
async def withdraw(request: Request, session: Session, user: ClientUser, order_id: int):
    order = await _own_order(session, order_id, user)
    return await _act(request, session, svc.withdraw_order(session, order, user), "Заказ снят, средства вернулись на баланс.", f"/client/orders/{order_id}")


async def _own_offer(session, offer_id: int, user) -> Offer:
    offer = await session.get(Offer, offer_id)
    if not offer or offer.order.client_id != user.id:
        raise HTTPException(404, "Отклик не найден")
    return offer


@router.post("/responses/{offer_id}/accept")
async def accept_response(request: Request, session: Session, user: ClientUser, offer_id: int):
    offer = await _own_offer(session, offer_id, user)
    return await _act(request, session, svc.accept_response(session, offer, user), "Исполнитель утверждён, подбор остановлен.", f"/client/orders/{offer.order_id}")


@router.post("/responses/{offer_id}/reject")
async def reject_response(request: Request, session: Session, user: ClientUser, offer_id: int):
    offer = await _own_offer(session, offer_id, user)
    return await _act(request, session, svc.reject_response(session, offer, user), "Отклик отклонён.", f"/client/orders/{offer.order_id}")


@router.post("/orders/{order_id}/accept")
async def accept_work(request: Request, session: Session, user: ClientUser, order_id: int):
    order = await _own_order(session, order_id, user)
    return await _act(request, session, svc.accept_work(session, order, user), "Работа принята, оплата перечислена исполнителю.", f"/client/orders/{order_id}")


@router.post("/orders/{order_id}/revision")
async def revision(request: Request, session: Session, user: ClientUser, order_id: int, comment: Annotated[str, Form()] = ""):
    order = await _own_order(session, order_id, user)
    return await _act(request, session, svc.request_revision(session, order, user, comment), "Заказ отправлен на доработку.", f"/client/orders/{order_id}")


@router.post("/orders/{order_id}/decline")
async def decline(request: Request, session: Session, user: ClientUser, order_id: int):
    order = await _own_order(session, order_id, user)
    return await _act(request, session, svc.decline_work(session, order, user), "Заказ отклонён. Открыто окно спора на 2 часа.", f"/client/orders/{order_id}")


@router.post("/orders/{order_id}/review")
async def review(request: Request, session: Session, user: ClientUser, order_id: int, score: Annotated[int, Form()] = 5, text: Annotated[str, Form()] = ""):
    order = await _own_order(session, order_id, user)
    return await _act(request, session, svc.leave_review(session, order, user, score, text), "Спасибо за отзыв!", f"/client/orders/{order_id}")
