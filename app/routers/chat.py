import json
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, WebSocket
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import ChatMessage, Offer, OfferStatus, Order, User
from app.redis import chat_channel, get_redis
from app.routers.notifications import pump
from app.security import SESSION_COOKIE, read_session_token
from app.web import TZ, CurrentUser, Session, redirect, render

router = APIRouter()

THREAD_OFFER_STATUSES = (OfferStatus.responded, OfferStatus.accepted)


async def _can_access(session: AsyncSession, user_id: int, order: Order, freelancer_id: int) -> bool:
    user = await session.get(User, user_id)
    if user and user.is_staff:
        return True  # поддержка читает переписку при разборе споров
    if user_id not in (order.client_id, freelancer_id):
        return False
    if order.freelancer_id == freelancer_id:
        return True
    # до назначения — переписка с откликнувшимися
    return bool(
        await session.scalar(
            select(Offer.id).where(Offer.order_id == order.id, Offer.freelancer_id == freelancer_id, Offer.status.in_(THREAD_OFFER_STATUSES))
        )
    )


@router.get("/chat")
async def threads(request: Request, session: Session, user: CurrentUser):
    rows = (
        await session.execute(
            select(Offer)
            .join(Order, Order.id == Offer.order_id)
            .where(
                Offer.status.in_(THREAD_OFFER_STATUSES),
                or_(Order.client_id == user.id, Offer.freelancer_id == user.id),
            )
            .order_by(Offer.responded_at.desc())
        )
    ).scalars().all()
    return await render(request, session, "chat_list.html", user, threads=rows)


@router.get("/chat/{order_id}/{freelancer_id}")
async def thread(request: Request, session: Session, user: CurrentUser, order_id: int, freelancer_id: int):
    order = await session.get(Order, order_id)
    if not order or not await _can_access(session, user.id, order, freelancer_id):
        raise HTTPException(404, "Чат не найден")
    messages = (
        await session.execute(
            select(ChatMessage).where(ChatMessage.order_id == order_id, ChatMessage.freelancer_id == freelancer_id).order_by(ChatMessage.created_at)
        )
    ).scalars().all()
    freelancer = await session.get(User, freelancer_id)
    partner = freelancer if user.id == order.client_id else order.client
    return await render(request, session, "chat.html", user, order=order, messages=messages, partner=partner, freelancer_id=freelancer_id)


@router.post("/chat/{order_id}/{freelancer_id}")
async def send(request: Request, session: Session, user: CurrentUser, order_id: int, freelancer_id: int, text: Annotated[str, Form()] = ""):
    order = await session.get(Order, order_id)
    if not order or not await _can_access(session, user.id, order, freelancer_id):
        raise HTTPException(404, "Чат не найден")
    text = text.strip()[:4000]
    if text:
        msg = ChatMessage(order_id=order_id, freelancer_id=freelancer_id, sender_id=user.id, text=text)
        session.add(msg)
        await session.commit()
        payload = {
            "type": "chat",
            "id": msg.id,
            "sender_id": user.id,
            "sender": user.full_name,
            "text": text,
            "time": msg.created_at.astimezone(TZ).strftime("%d.%m %H:%M"),
        }
        await get_redis().publish(chat_channel(order_id, freelancer_id), json.dumps(payload))
    if request.headers.get("x-requested-with") == "fetch":
        return {"ok": True}
    return redirect(f"/chat/{order_id}/{freelancer_id}")


@router.websocket("/ws/chat/{order_id}/{freelancer_id}")
async def ws_chat(websocket: WebSocket, order_id: int, freelancer_id: int):
    user_id = read_session_token(websocket.cookies.get(SESSION_COOKIE, ""))
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        allowed = bool(user_id and order and await _can_access(session, user_id, order, freelancer_id))
    if not allowed:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    await pump(websocket, chat_channel(order_id, freelancer_id))
