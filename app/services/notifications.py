"""Уведомления: запись в историю + push на сайт (Redis pub/sub → WebSocket) + email.

Push и письма отправляются только после коммита транзакции — см. commit_and_push().
"""

import json
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, User
from app.redis import get_redis, user_channel
from app.services.mail import send_email


class Kind(StrEnum):
    # заказчик
    new_response = "new_response"
    deadline_soon = "deadline_soon"
    work_submitted = "work_submitted"
    # исполнитель
    new_offer = "new_offer"
    selected = "selected"
    work_accepted = "work_accepted"
    work_declined = "work_declined"
    revision = "revision"
    # общие
    dispute = "dispute"
    moderation = "moderation"
    finance = "finance"
    support = "support"


TITLES = {
    Kind.new_response: "Новый отклик",
    Kind.deadline_soon: "Скоро срок сдачи",
    Kind.work_submitted: "Работа отправлена",
    Kind.new_offer: "Новое предложение заказа",
    Kind.selected: "Вас выбрали исполнителем",
    Kind.work_accepted: "Работа принята",
    Kind.work_declined: "Работа отклонена",
    Kind.revision: "Требуются правки",
    Kind.dispute: "Спор",
    Kind.moderation: "Модерация",
    Kind.finance: "Финансы",
    Kind.support: "Поддержка",
}

_PENDING = "pending_push"


def notify(session: AsyncSession, user: User, kind: Kind, text: str, link: str | None = None) -> None:
    session.add(Notification(user_id=user.id, kind=kind.value, text=text, link=link))
    session.info.setdefault(_PENDING, []).append(
        {
            "user_id": user.id,
            "email": user.email if user.email_notifications else None,
            "kind": kind.value,
            "title": TITLES[kind],
            "text": text,
            "link": link,
        }
    )


async def commit_and_push(session: AsyncSession) -> None:
    await session.commit()
    pending = session.info.pop(_PENDING, [])
    r = get_redis()
    for item in pending:
        payload = {k: item[k] for k in ("kind", "title", "text", "link")}
        await r.publish(user_channel(item["user_id"]), json.dumps({"type": "notification", **payload}))
        if item["email"]:
            from app.config import settings

            link = f"\n\n{settings.base_url}{item['link']}" if item["link"] else ""
            await send_email(item["email"], item["title"], item["text"] + link)
