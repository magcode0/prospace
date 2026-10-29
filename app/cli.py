"""Служебные команды.

  python -m app.cli make-staff <username>   — выдать права поддержки
  python -m app.cli demo                    — демо-данные: заказчик, 12 исполнителей, сотрудник
"""

import asyncio
import random
import sys
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Category, Role, User
from app.security import hash_password

DEMO_PASSWORD = "Prospace#2026"


async def make_staff(username: str) -> None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.username == username))
        if not user:
            sys.exit(f"Пользователь {username} не найден")
        user.is_staff = True
        await session.commit()
        print(f"{username} теперь сотрудник поддержки")


async def demo() -> None:
    async with SessionLocal() as session:
        if await session.scalar(select(User).where(User.username == "client")):
            print("Демо-данные уже есть")
            return
        cats = (await session.execute(select(Category))).scalars().all()

        def user(username, first, last, role, **kw):
            return User(
                username=username, first_name=first, last_name=last, email=f"{username}@prospace.local",
                password_hash=hash_password(DEMO_PASSWORD), role=role, is_email_verified=True, **kw,
            )

        session.add(user("client", "Анна", "Заказова", Role.client, balance=Decimal("50000"), about="Продуктовая команда, ищем подрядчиков"))
        session.add(user("support", "Сергей", "Поддержкин", Role.client, is_staff=True))
        names = ["Иван", "Мария", "Пётр", "Ольга", "Дмитрий", "Елена", "Алексей", "Наталья", "Кирилл", "Светлана", "Максим", "Юлия"]
        rnd = random.Random(7)
        for i, name in enumerate(names):
            experienced = i < 8
            session.add(user(
                f"dev{i + 1}", name, "Исполнителев", Role.freelancer,
                balance=Decimal("1000"),
                skills="Python, FastAPI, PostgreSQL" if i % 2 == 0 else "Figma, UI/UX",
                about="Делаю быстро и аккуратно",
                completed_orders=rnd.randint(3, 40) if experienced else rnd.randint(0, 2),
                rating=round(rnd.uniform(3.8, 5.0), 2) if experienced else 0.0,
                rating_count=rnd.randint(3, 30) if experienced else 0,
                offers_received=rnd.randint(10, 60), offers_reacted=rnd.randint(5, 10),
                work_hours_start=0, work_hours_end=0,
                categories=cats,
            ))
        await session.commit()
        print(f"Готово. Логины: client, support, dev1…dev12 — пароль {DEMO_PASSWORD}")


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["make-staff", username]:
            asyncio.run(make_staff(username))
        case ["demo"]:
            asyncio.run(demo())
        case _:
            print(__doc__)
