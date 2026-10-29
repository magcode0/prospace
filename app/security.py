import re
import secrets
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from app.config import settings
from app.models import utcnow

_hasher = PasswordHasher()

SESSION_COOKIE = "prospace_session"
SESSION_DAYS = 14

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё\- ]{1,64}$")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerificationError:
        return False


def password_problems(password: str) -> list[str]:
    """Сложный пароль: ≥ 8 символов, строчная, заглавная, цифра, спецсимвол."""
    problems = []
    if len(password) < 8:
        problems.append("не короче 8 символов")
    if not re.search(r"[a-zа-яё]", password):
        problems.append("строчная буква")
    if not re.search(r"[A-ZА-ЯЁ]", password):
        problems.append("заглавная буква")
    if not re.search(r"\d", password):
        problems.append("цифра")
    if not re.search(r"[^\w\s]|_", password):
        problems.append("спецсимвол")
    return problems


def create_session_token(user_id: int) -> str:
    payload = {"sub": str(user_id), "exp": utcnow() + timedelta(days=SESSION_DAYS)}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def read_session_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def generate_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"
