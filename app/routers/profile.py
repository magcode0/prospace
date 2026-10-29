import io
import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select

from app.config import settings
from app.models import BusyStatus, Category, Order, OrderStatus, Review, Role, User
from app.security import NAME_RE, hash_password, password_problems, verify_password
from app.services.codes import check_code, issue_code
from app.services.mail import send_code
from app.web import CurrentUser, OptionalUser, Session, flash, home_url, redirect, render

router = APIRouter()

AVATAR_DIR = Path(settings.media_dir) / "avatars"
MAX_AVATAR_BYTES = 5 * 1024 * 1024


@router.get("/profile")
async def settings_page(request: Request, session: Session, user: CurrentUser):
    categories = (await session.execute(select(Category).order_by(Category.name))).scalars().all()
    return await render(request, session, "profile/settings.html", user, categories=categories)


@router.post("/profile")
async def save_profile(
    request: Request,
    session: Session,
    user: CurrentUser,
    first_name: Annotated[str, Form()] = "",
    last_name: Annotated[str, Form()] = "",
    about: Annotated[str, Form()] = "",
    skills: Annotated[str, Form()] = "",
    category_ids: Annotated[list[int], Form()] = [],  # noqa: B006
    busy_status: Annotated[str, Form()] = "free",
    min_price: Annotated[str, Form()] = "1000",
    work_hours_start: Annotated[int, Form()] = 9,
    work_hours_end: Annotated[int, Form()] = 21,
    email_notifications: Annotated[bool, Form()] = False,
):
    errors = []
    if not NAME_RE.match(first_name.strip()) or not NAME_RE.match(last_name.strip()):
        errors.append("Имя и фамилия обязательны: только буквы, пробел и дефис")
    if len(about) > 200:
        errors.append("«О себе» — не более 200 символов")
    try:
        price = Decimal(min_price.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        price = Decimal("0")
    if user.role == Role.freelancer and price < settings.min_order_price:
        errors.append(f"Минимальная сумма заказа — не меньше {settings.min_order_price:.0f} ₽")
    if not (0 <= work_hours_start <= 23 and 0 <= work_hours_end <= 23):
        errors.append("Рабочие часы — от 0 до 23")
    if errors:
        for e in errors:
            flash(request, e, "error")
        return redirect("/profile")

    user.first_name = first_name.strip()
    user.last_name = last_name.strip()
    user.about = about.strip()
    user.email_notifications = email_notifications
    if user.role == Role.freelancer:
        user.skills = skills.strip()[:500]
        user.busy_status = BusyStatus(busy_status) if busy_status in ("free", "busy") else BusyStatus.free
        user.min_price = price
        user.work_hours_start = work_hours_start
        user.work_hours_end = work_hours_end
        user.categories = list((await session.execute(select(Category).where(Category.id.in_(category_ids)))).scalars())
    await session.commit()
    flash(request, "Профиль сохранён", "success")
    return redirect("/profile")


def _process_avatar(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((256, 256), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


@router.post("/profile/avatar")
async def upload_avatar(request: Request, session: Session, user: CurrentUser, avatar: Annotated[UploadFile, File()]):
    data = await avatar.read(MAX_AVATAR_BYTES + 1)
    if len(data) > MAX_AVATAR_BYTES:
        flash(request, "Файл больше 5 МБ", "error")
        return redirect("/profile")
    try:
        jpeg = await run_in_threadpool(_process_avatar, data)
    except (UnidentifiedImageError, OSError):
        flash(request, "Загрузите изображение JPG, PNG или WEBP", "error")
        return redirect("/profile")
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{user.id}_{secrets.token_hex(6)}.jpg"
    (AVATAR_DIR / name).write_bytes(jpeg)
    if user.avatar:
        (AVATAR_DIR / Path(user.avatar).name).unlink(missing_ok=True)
    user.avatar = f"/media/avatars/{name}"
    await session.commit()
    flash(request, "Фото обновлено", "success")
    return redirect("/profile")


@router.post("/profile/role")
async def switch_role(request: Request, session: Session, user: CurrentUser):
    user.role = Role.freelancer if user.role == Role.client else Role.client
    await session.commit()
    flash(request, "Кабинет переключён: " + ("исполнитель" if user.role == Role.freelancer else "заказчик"), "success")
    return redirect(home_url(user))


@router.post("/profile/password/code")
async def password_code(request: Request, user: CurrentUser):
    code = await issue_code("change", user.email)
    await send_code(user.email, "смена пароля", code)
    flash(request, f"Код для смены пароля отправлен на {user.email}")
    return redirect("/profile#password")


@router.post("/profile/password")
async def change_password(
    request: Request,
    session: Session,
    user: CurrentUser,
    current: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password2: Annotated[str, Form()] = "",
):
    error = None
    if not verify_password(current, user.password_hash):
        error = "Текущий пароль указан неверно"
    elif problems := password_problems(password):
        error = "Пароль слишком простой. Нужно: " + ", ".join(problems)
    elif password != password2:
        error = "Пароли не совпадают"
    elif not await check_code("change", user.email, code):
        error = "Неверный или просроченный код из письма"
    if error:
        flash(request, error, "error")
        return redirect("/profile#password")
    user.password_hash = hash_password(password)
    await session.commit()
    flash(request, "Пароль изменён", "success")
    return redirect("/profile")


@router.get("/users/{username}")
async def public_profile(request: Request, session: Session, viewer: OptionalUser, username: str):
    target = await session.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if not target or not target.is_email_verified:
        raise HTTPException(404, "Пользователь не найден")
    reviews = (
        await session.execute(select(Review).where(Review.target_id == target.id).order_by(Review.created_at.desc()).limit(20))
    ).scalars().all()
    placed = await session.scalar(select(func.count()).where(Order.client_id == target.id, Order.status != OrderStatus.rejected))
    return await render(request, session, "profile/public.html", viewer, target=target, reviews=reviews, placed=placed)
