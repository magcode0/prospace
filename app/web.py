"""Общее для роутеров: шаблоны, текущий пользователь, flash-сообщения, редиректы."""

from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import Notification, Role, User, utcnow
from app.redis import get_redis
from app.security import SESSION_COOKIE, read_session_token

templates = Jinja2Templates(directory="app/templates")
TZ = ZoneInfo(settings.timezone)


def fmt_dt(value: datetime | None, pattern: str = "%d.%m.%Y %H:%M") -> str:
    return value.astimezone(TZ).strftime(pattern) if value else "—"


def fmt_money(value) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".00", "") + " ₽"


def is_online(user: User) -> bool:
    return utcnow() - user.last_seen_at < timedelta(minutes=settings.online_threshold_minutes)


def seen_text(user: User) -> str:
    if is_online(user):
        return "в сети"
    return f"был(а) в сети {fmt_dt(user.last_seen_at)}"


templates.env.filters["dt"] = fmt_dt
templates.env.filters["money"] = fmt_money
templates.env.globals.update(is_online=is_online, seen_text=seen_text, settings=settings)

Session = Annotated[AsyncSession, Depends(get_session)]


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "text": message})


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


class LoginRequired(Exception):
    pass


async def _touch_last_seen(session: AsyncSession, user: User) -> None:
    # пишем в БД не чаще раза в минуту
    if await get_redis().set(f"seen:{user.id}", 1, nx=True, ex=60):
        user.last_seen_at = utcnow()
        await session.commit()


async def optional_user(request: Request, session: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    user_id = read_session_token(token) if token else None
    if not user_id:
        return None
    user = await session.get(User, user_id)
    if not user or not user.is_email_verified or user.is_blocked:
        return None
    await _touch_last_seen(session, user)
    return user


async def current_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    if user is None:
        raise LoginRequired
    return user


async def client_user(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != Role.client:
        raise HTTPException(403, "Раздел доступен в кабинете заказчика — переключите роль в настройках профиля")
    return user


async def freelancer_user(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != Role.freelancer:
        raise HTTPException(403, "Раздел доступен в кабинете исполнителя — переключите роль в настройках профиля")
    return user


async def staff_user(user: Annotated[User, Depends(current_user)]) -> User:
    if not user.is_staff:
        raise HTTPException(403, "Только для сотрудников поддержки")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
ClientUser = Annotated[User, Depends(client_user)]
FreelancerUser = Annotated[User, Depends(freelancer_user)]
StaffUser = Annotated[User, Depends(staff_user)]
OptionalUser = Annotated[User | None, Depends(optional_user)]


async def render(request: Request, session: AsyncSession, name: str, user: User | None = None, status_code: int = 200, **ctx):
    unread = 0
    if user:
        unread = await session.scalar(
            select(func.count()).where(Notification.user_id == user.id, Notification.is_read.is_(False))
        )
    messages = request.session.pop("flash", [])
    return templates.TemplateResponse(
        request, name, {"user": user, "unread": unread, "flashes": messages, **ctx}, status_code=status_code
    )


def home_url(user: User) -> str:
    return "/client/orders" if user.role == Role.client else "/freelancer/orders"


# ---------- геймификация исполнителя ----------

XP_PER_LEVEL = 300

ACHIEVEMENTS = [
    ("first", "Первый заказ", lambda u: u.completed_orders >= 1),
    ("five", "Пять заказов", lambda u: u.completed_orders >= 5),
    ("star", "Звезда · рейтинг 4.8+", lambda u: u.rating >= 4.8 and u.rating_count >= 3),
    ("fast", "Всегда на связи", lambda u: u.offers_received >= 5 and u.offers_reacted / u.offers_received >= 0.8),
    ("pro", "Опытный · 20 заказов", lambda u: u.completed_orders >= 20),
]


def progress(user: User) -> dict:
    """Уровень и достижения: выполненные заказы, оценки и реакция на предложения."""
    xp = user.completed_orders * 100 + user.rating_count * 20 + user.offers_reacted * 5
    return {
        "level": xp // XP_PER_LEVEL + 1,
        "xp": xp % XP_PER_LEVEL,
        "xp_next": XP_PER_LEVEL,
        "pct": round(xp % XP_PER_LEVEL / XP_PER_LEVEL * 100),
        "achievements": [(title, check(user)) for _, title, check in ACHIEVEMENTS],
    }


def month_bars(rows: list[tuple[datetime, object]], months: int = 6) -> list[int]:
    """Высоты столбиков мини-графика (в %) за последние N месяцев."""
    now = utcnow().astimezone(TZ)
    keys = []
    y, m = now.year, now.month
    for _ in range(months):
        keys.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    keys.reverse()
    sums = dict.fromkeys(keys, 0.0)
    for created, amount in rows:
        local = created.astimezone(TZ)
        if (local.year, local.month) in sums:
            sums[(local.year, local.month)] += abs(float(amount))
    top = max(sums.values()) or 1
    return [max(8, round(v / top * 100)) for v in sums.values()]


templates.env.globals.update(progress=progress)


MONTHS_GENITIVE = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")


def day_label(day, today) -> str:
    """«Сегодня», «Вчера» или «21 октября» — заголовки групп в лентах."""
    if day == today:
        return "Сегодня"
    if day == today - timedelta(days=1):
        return "Вчера"
    return f"{day.day} {MONTHS_GENITIVE[day.month - 1]}" + ("" if day.year == today.year else f" {day.year}")
