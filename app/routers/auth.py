from typing import Annotated

from fastapi import APIRouter, Form, Request
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, or_, select

from app.models import Role, User
from app.security import (
    NAME_RE,
    SESSION_COOKIE,
    SESSION_DAYS,
    USERNAME_RE,
    create_session_token,
    hash_password,
    password_problems,
    verify_password,
)
from app.services.codes import check_code, issue_code
from app.services.mail import send_code
from app.web import OptionalUser, Session, flash, home_url, redirect, render

router = APIRouter()
_email = TypeAdapter(EmailStr)


def _login_response(user: User):
    resp = redirect(home_url(user))
    resp.set_cookie(
        SESSION_COOKIE,
        create_session_token(user.id),
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
    )
    return resp


async def _send_verification(email: str) -> None:
    code = await issue_code("verify", email)
    await send_code(email, "подтверждение email", code)


@router.get("/register")
async def register_page(request: Request, session: Session, user: OptionalUser):
    if user:
        return redirect(home_url(user))
    return await render(request, session, "auth/register.html", form={}, errors={})


@router.post("/register")
async def register(
    request: Request,
    session: Session,
    first_name: Annotated[str, Form()] = "",
    last_name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password2: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
):
    form = {"first_name": first_name.strip(), "last_name": last_name.strip(), "email": email.strip().lower(), "username": username.strip(), "role": role}
    errors: dict[str, str] = {}
    if not form["first_name"]:
        errors["first_name"] = "Обязательное поле"
    elif not NAME_RE.match(form["first_name"]):
        errors["first_name"] = "Только буквы, пробел и дефис"
    if not form["last_name"]:
        errors["last_name"] = "Обязательное поле"
    elif not NAME_RE.match(form["last_name"]):
        errors["last_name"] = "Только буквы, пробел и дефис"
    try:
        _email.validate_python(form["email"])
    except ValidationError:
        errors["email"] = "Некорректный email"
    if not USERNAME_RE.match(form["username"]):
        errors["username"] = "3–32 символа: латиница, цифры, _"
    if problems := password_problems(password):
        errors["password"] = "Нужно: " + ", ".join(problems)
    elif password != password2:
        errors["password2"] = "Пароли не совпадают"
    if role not in (Role.client.value, Role.freelancer.value):
        errors["role"] = "Выберите, кто вы"

    existing_email = None
    if "email" not in errors:
        existing_email = await session.scalar(select(User).where(User.email == form["email"]))
        if existing_email and existing_email.is_email_verified:
            errors["email"] = "Этот email уже зарегистрирован"
    if "username" not in errors:
        taken = await session.scalar(select(User).where(func.lower(User.username) == form["username"].lower()))
        if taken and taken is not existing_email:
            errors["username"] = "Этот username занят"

    if errors:
        return await render(request, session, "auth/register.html", form=form, errors=errors, status_code=422)

    # незавершённую регистрацию с тем же email перезаписываем
    user = existing_email or User(email=form["email"])
    user.first_name = form["first_name"]
    user.last_name = form["last_name"]
    user.username = form["username"]
    user.password_hash = hash_password(password)
    user.role = Role(role)
    user.is_email_verified = False
    session.add(user)
    await session.commit()

    await _send_verification(user.email)
    request.session["verify_email"] = user.email
    flash(request, f"Мы отправили код подтверждения на {user.email}")
    return redirect("/verify")


@router.get("/verify")
async def verify_page(request: Request, session: Session):
    email = request.session.get("verify_email")
    if not email:
        return redirect("/login")
    return await render(request, session, "auth/verify.html", email=email, error=None)


@router.post("/verify")
async def verify(request: Request, session: Session, code: Annotated[str, Form()] = ""):
    email = request.session.get("verify_email")
    if not email:
        return redirect("/login")
    user = await session.scalar(select(User).where(User.email == email))
    if not user or not await check_code("verify", email, code):
        return await render(request, session, "auth/verify.html", email=email, error="Неверный или просроченный код", status_code=422)
    user.is_email_verified = True
    await session.commit()
    request.session.pop("verify_email", None)
    flash(request, "Email подтверждён. Добро пожаловать в ProSpace!", "success")
    return _login_response(user)


@router.post("/verify/resend")
async def verify_resend(request: Request):
    email = request.session.get("verify_email")
    if email:
        await _send_verification(email)
        flash(request, "Новый код отправлен")
    return redirect("/verify")


@router.get("/login")
async def login_page(request: Request, session: Session, user: OptionalUser):
    if user:
        return redirect(home_url(user))
    return await render(request, session, "auth/login.html", login="", error=None)


@router.post("/login")
async def login(request: Request, session: Session, login: Annotated[str, Form()] = "", password: Annotated[str, Form()] = ""):
    ident = login.strip()
    user = await session.scalar(
        select(User).where(or_(func.lower(User.username) == ident.lower(), User.email == ident.lower()))
    )
    if not user or not verify_password(password, user.password_hash):
        return await render(request, session, "auth/login.html", login=ident, error="Неверный логин или пароль", status_code=422)
    if user.is_blocked:
        reason = f" Причина: {user.blocked_reason}" if user.blocked_reason else ""
        return await render(request, session, "auth/login.html", login=ident, error=f"Аккаунт заблокирован.{reason} Напишите в поддержку.", status_code=403)
    if not user.is_email_verified:
        await _send_verification(user.email)
        request.session["verify_email"] = user.email
        flash(request, "Подтвердите email — мы отправили новый код")
        return redirect("/verify")
    return _login_response(user)


@router.post("/logout")
async def logout():
    resp = redirect("/")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/password/forgot")
async def forgot_page(request: Request, session: Session):
    return await render(request, session, "auth/forgot.html")


@router.post("/password/forgot")
async def forgot(request: Request, session: Session, email: Annotated[str, Form()] = ""):
    email = email.strip().lower()
    user = await session.scalar(select(User).where(User.email == email))
    if user:
        code = await issue_code("reset", email)
        await send_code(email, "сброс пароля", code)
    # одинаковый ответ, чтобы не раскрывать, есть ли такой email
    request.session["reset_email"] = email
    flash(request, "Если такой email зарегистрирован, мы отправили на него код")
    return redirect("/password/reset")


@router.get("/password/reset")
async def reset_page(request: Request, session: Session):
    if not request.session.get("reset_email"):
        return redirect("/password/forgot")
    return await render(request, session, "auth/reset.html", error=None)


@router.post("/password/reset")
async def reset(
    request: Request,
    session: Session,
    code: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password2: Annotated[str, Form()] = "",
):
    email = request.session.get("reset_email")
    if not email:
        return redirect("/password/forgot")
    error = None
    if problems := password_problems(password):
        error = "Пароль слишком простой. Нужно: " + ", ".join(problems)
    elif password != password2:
        error = "Пароли не совпадают"
    user = await session.scalar(select(User).where(User.email == email))
    if not error and (not user or not await check_code("reset", email, code)):
        error = "Неверный или просроченный код"
    if error:
        return await render(request, session, "auth/reset.html", error=error, status_code=422)
    user.password_hash = hash_password(password)
    user.is_email_verified = True
    await session.commit()
    request.session.pop("reset_email", None)
    flash(request, "Пароль изменён. Войдите с новым паролем.", "success")
    return redirect("/login")
