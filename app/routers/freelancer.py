from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import select

from app.models import (
    ACTIVE_WORK_STATUSES,
    Dispute,
    Offer,
    OfferStatus,
    Order,
    OrderStatus,
    Review,
    Transaction,
    TxKind,
    utcnow,
)
from app.services import orders as svc
from app.services.notifications import commit_and_push
from app.web import FreelancerUser, Session, flash, month_bars, redirect, render

router = APIRouter(prefix="/freelancer")


async def _bars(session, user) -> list[int]:
    rows = (
        await session.execute(
            select(Transaction.created_at, Transaction.amount).where(Transaction.user_id == user.id, Transaction.kind == TxKind.payout)
        )
    ).all()
    return month_bars(rows)


@router.get("/orders")
async def available(request: Request, session: Session, user: FreelancerUser):
    """Доступные заказы — открывается по умолчанию."""
    svc.refresh_credits(user)
    await session.commit()
    offers = (
        await session.execute(
            select(Offer)
            .where(Offer.freelancer_id == user.id, Offer.status == OfferStatus.sent, Offer.expires_at > utcnow())
            .order_by(Offer.expires_at)
        )
    ).scalars().all()
    responded = (
        await session.execute(
            select(Offer).where(Offer.freelancer_id == user.id, Offer.status == OfferStatus.responded).order_by(Offer.responded_at.desc())
        )
    ).scalars().all()
    return await render(request, session, "freelancer/available.html", user, offers=offers, responded=responded, tab="available", bars=await _bars(session, user))


@router.get("/orders/work")
async def in_work(request: Request, session: Session, user: FreelancerUser):
    rows = (
        await session.execute(
            select(Order)
            .where(Order.freelancer_id == user.id, Order.status.in_((*ACTIVE_WORK_STATUSES, OrderStatus.declined, OrderStatus.disputed)))
            .order_by(Order.deadline)
        )
    ).scalars().all()
    return await render(request, session, "freelancer/work.html", user, orders=rows, tab="work", bars=await _bars(session, user))


@router.get("/orders/history")
async def history(request: Request, session: Session, user: FreelancerUser):
    rows = (
        await session.execute(
            select(Order)
            .where(Order.freelancer_id == user.id, Order.status.in_((OrderStatus.completed, OrderStatus.cancelled)))
            .order_by(Order.closed_at.desc())
        )
    ).scalars().all()
    offers = (
        await session.execute(
            select(Offer)
            .where(Offer.freelancer_id == user.id, Offer.status.in_((OfferStatus.declined, OfferStatus.expired, OfferStatus.rejected, OfferStatus.closed)))
            .order_by(Offer.sent_at.desc())
            .limit(50)
        )
    ).scalars().all()
    return await render(request, session, "freelancer/history.html", user, orders=rows, offers=offers, tab="history", bars=await _bars(session, user))


async def _my_offer(session, offer_id: int, user) -> Offer:
    offer = await session.get(Offer, offer_id)
    if not offer or offer.freelancer_id != user.id:
        raise HTTPException(404, "Предложение не найдено")
    return offer


@router.get("/offers/{offer_id}")
async def offer_detail(request: Request, session: Session, user: FreelancerUser, offer_id: int):
    offer = await _my_offer(session, offer_id, user)
    svc.refresh_credits(user)
    await session.commit()
    return await render(request, session, "freelancer/offer.html", user, offer=offer, order=offer.order)


@router.post("/offers/{offer_id}/respond")
async def respond(request: Request, session: Session, user: FreelancerUser, offer_id: int, message: Annotated[str, Form()] = ""):
    offer = await _my_offer(session, offer_id, user)
    try:
        await svc.respond(session, offer, user, message)
    except svc.DomainError as exc:
        await session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/freelancer/offers/{offer_id}")
    await commit_and_push(session)
    flash(request, "Отклик отправлен заказчику. 50 ₽ вернутся, если вас не выберут.", "success")
    return redirect("/freelancer/orders")


@router.post("/offers/{offer_id}/decline")
async def decline(request: Request, session: Session, user: FreelancerUser, offer_id: int):
    offer = await _my_offer(session, offer_id, user)
    try:
        await svc.decline_offer(session, offer, user)
    except svc.DomainError as exc:
        await session.rollback()
        flash(request, str(exc), "error")
        return redirect("/freelancer/orders")
    await commit_and_push(session)
    flash(request, "Вы отказались от заказа.")
    return redirect("/freelancer/orders")


async def _my_work(session, order_id: int, user) -> Order:
    order = await session.get(Order, order_id)
    if not order or order.freelancer_id != user.id:
        raise HTTPException(404, "Заказ не найден")
    return order


# /freelancer/work/{id} — не под /freelancer/orders, чтобы не конфликтовать с вкладками
@router.get("/work/{order_id}")
async def work_detail(request: Request, session: Session, user: FreelancerUser, order_id: int):
    order = await _my_work(session, order_id, user)
    dispute = await session.scalar(select(Dispute).where(Dispute.order_id == order.id))
    reviewed = await session.scalar(select(Review.id).where(Review.order_id == order.id, Review.author_id == user.id))
    return await render(request, session, "freelancer/work_detail.html", user, order=order, dispute=dispute, reviewed=reviewed, now=utcnow())


async def _act(request, session, coro, ok: str, back: str):
    try:
        await coro
    except svc.DomainError as exc:
        await session.rollback()
        flash(request, str(exc), "error")
        return redirect(back)
    await commit_and_push(session)
    flash(request, ok, "success")
    return redirect(back)


@router.post("/work/{order_id}/submit")
async def submit(request: Request, session: Session, user: FreelancerUser, order_id: int, text: Annotated[str, Form()] = ""):
    order = await _my_work(session, order_id, user)
    return await _act(request, session, svc.submit_work(session, order, user, text), "Работа отправлена заказчику.", f"/freelancer/work/{order_id}")


@router.post("/work/{order_id}/dispute")
async def dispute(request: Request, session: Session, user: FreelancerUser, order_id: int, reason: Annotated[str, Form()] = ""):
    order = await _my_work(session, order_id, user)
    return await _act(request, session, svc.open_dispute(session, order, user, reason), "Спор открыт. Поддержка рассмотрит его в течение 24 часов.", f"/freelancer/work/{order_id}")


@router.post("/work/{order_id}/review")
async def review(request: Request, session: Session, user: FreelancerUser, order_id: int, score: Annotated[int, Form()] = 5, text: Annotated[str, Form()] = ""):
    order = await _my_work(session, order_id, user)
    return await _act(request, session, svc.leave_review(session, order, user, score, text), "Спасибо за отзыв!", f"/freelancer/work/{order_id}")
