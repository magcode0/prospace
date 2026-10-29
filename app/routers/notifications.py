import asyncio

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select, update

from app.models import Notification, utcnow
from app.redis import redis, user_channel
from app.security import SESSION_COOKIE, read_session_token
from app.services.notifications import TITLES
from app.web import TZ, CurrentUser, Session, day_label, render

router = APIRouter()


FILTERS = {
    "all": ("Все", None),
    "orders": ("Заказы", ("new_offer", "selected", "work_submitted", "work_accepted", "work_declined", "revision", "deadline_soon", "moderation")),
    "responses": ("Отклики", ("new_response",)),
    "money": ("Деньги и споры", ("finance", "dispute")),
    "support": ("Поддержка", ("support",)),
}


@router.get("/notifications")
async def history(request: Request, session: Session, user: CurrentUser, f: str = "all"):
    f = f if f in FILTERS else "all"
    stmt = select(Notification).where(Notification.user_id == user.id)
    if kinds := FILTERS[f][1]:
        stmt = stmt.where(Notification.kind.in_(kinds))
    rows = (await session.execute(stmt.order_by(Notification.created_at.desc()).limit(200))).scalars().all()
    unread_ids = {n.id for n in rows if not n.is_read}
    total_unread = await session.scalar(
        select(func.count()).where(Notification.user_id == user.id, Notification.is_read.is_(False))
    )
    if unread_ids:
        # прочитанными считаем только показанные — с фильтром остальные остаются новыми
        await session.execute(update(Notification).where(Notification.id.in_(unread_ids)).values(is_read=True))
        await session.commit()

    today = utcnow().astimezone(TZ).date()
    groups: list[tuple[str, list[Notification]]] = []
    for n in rows:
        label = day_label(n.created_at.astimezone(TZ).date(), today)
        if not groups or groups[-1][0] != label:
            groups.append((label, []))
        groups[-1][1].append(n)
    return await render(
        request, session, "notifications.html", user,
        groups=groups, unread_ids=unread_ids, total_unread=total_unread, filters=FILTERS, current=f, titles=TITLES,
    )


async def pump(websocket: WebSocket, channel: str) -> None:
    """Пересылает сообщения из Redis pub/sub в WebSocket, пока клиент подключён."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    async def reader():
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"])

    task = asyncio.create_task(reader())
    try:
        while True:
            await websocket.receive_text()  # ping от клиента / ожидание разрыва
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


@router.websocket("/ws/notifications")
async def ws_notifications(websocket: WebSocket):
    user_id = read_session_token(websocket.cookies.get(SESSION_COOKIE, ""))
    if not user_id:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await pump(websocket, user_channel(user_id))
