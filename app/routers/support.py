from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import select

from app.models import Order, SupportMessage, SupportTicket, TicketStatus, User
from app.services.notifications import Kind, commit_and_push, notify
from app.web import CurrentUser, Session, flash, redirect, render

router = APIRouter(prefix="/support")


@router.get("")
async def tickets(request: Request, session: Session, user: CurrentUser, order: int | None = None):
    rows = (
        await session.execute(select(SupportTicket).where(SupportTicket.user_id == user.id).order_by(SupportTicket.created_at.desc()))
    ).scalars().all()
    return await render(request, session, "support/tickets.html", user, tickets=rows, order_id=order)


@router.post("")
async def create_ticket(
    request: Request,
    session: Session,
    user: CurrentUser,
    subject: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
    order_id: Annotated[int | None, Form()] = None,
):
    if len(subject.strip()) < 3 or len(text.strip()) < 10:
        flash(request, "Укажите тему и опишите вопрос (от 10 символов)", "error")
        return redirect("/support")
    if order_id:
        order = await session.get(Order, order_id)
        if not order or user.id not in (order.client_id, order.freelancer_id):
            order_id = None
    ticket = SupportTicket(user_id=user.id, subject=subject.strip()[:200], order_id=order_id)
    session.add(ticket)
    await session.flush()
    session.add(SupportMessage(ticket_id=ticket.id, sender_id=user.id, text=text.strip()))
    for member in (await session.execute(select(User).where(User.is_staff.is_(True)))).scalars():
        notify(session, member, Kind.support, f"Новое обращение #{ticket.id}: {ticket.subject}", f"/support/{ticket.id}")
    await commit_and_push(session)
    flash(request, "Обращение отправлено. Мы ответим в ближайшее время.", "success")
    return redirect(f"/support/{ticket.id}")


async def _ticket(session, ticket_id: int, user: User) -> SupportTicket:
    ticket = await session.get(SupportTicket, ticket_id)
    if not ticket or (ticket.user_id != user.id and not user.is_staff):
        raise HTTPException(404, "Обращение не найдено")
    return ticket


@router.get("/{ticket_id}")
async def ticket_page(request: Request, session: Session, user: CurrentUser, ticket_id: int):
    ticket = await _ticket(session, ticket_id, user)
    messages = (
        await session.execute(select(SupportMessage).where(SupportMessage.ticket_id == ticket.id).order_by(SupportMessage.created_at))
    ).scalars().all()
    return await render(request, session, "support/ticket.html", user, ticket=ticket, messages=messages)


@router.post("/{ticket_id}")
async def reply(request: Request, session: Session, user: CurrentUser, ticket_id: int, text: Annotated[str, Form()] = "", close: Annotated[bool, Form()] = False):
    ticket = await _ticket(session, ticket_id, user)
    if text.strip():
        session.add(SupportMessage(ticket_id=ticket.id, sender_id=user.id, text=text.strip()))
        if user.is_staff and user.id != ticket.user_id:
            ticket.status = TicketStatus.answered
            notify(session, ticket.user, Kind.support, f"Поддержка ответила на обращение «{ticket.subject}»", f"/support/{ticket.id}")
        else:
            ticket.status = TicketStatus.open
    if close:
        ticket.status = TicketStatus.closed
    await commit_and_push(session)
    return redirect(f"/support/{ticket.id}")
