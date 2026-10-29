"""Движения денег по внутренним счетам биржи. Каждое движение пишется в журнал transactions."""

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Transaction, TxKind, User


class InsufficientFunds(Exception):
    def __init__(self, need: Decimal, have: Decimal):
        super().__init__(f"Недостаточно средств: нужно {need} ₽, на балансе {have} ₽")
        self.need = need
        self.have = have


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def lock_user(session: AsyncSession, user_id: int) -> User:
    stmt = select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).unique().scalar_one()


async def charge(session: AsyncSession, user: User, amount: Decimal, kind: TxKind, order_id: int | None = None) -> None:
    """Списать с баланса пользователя. user должен быть заблокирован через lock_user()."""
    amount = money(amount)
    if user.balance < amount:
        raise InsufficientFunds(amount, user.balance)
    user.balance = money(user.balance - amount)
    session.add(Transaction(user_id=user.id, order_id=order_id, kind=kind, amount=-amount))


async def credit(session: AsyncSession, user: User, amount: Decimal, kind: TxKind, order_id: int | None = None) -> None:
    amount = money(amount)
    if amount <= 0:
        return
    user.balance = money(user.balance + amount)
    session.add(Transaction(user_id=user.id, order_id=order_id, kind=kind, amount=amount))


def platform_income(session: AsyncSession, amount: Decimal, kind: TxKind, order_id: int | None = None) -> None:
    amount = money(amount)
    if amount > 0:
        session.add(Transaction(user_id=None, order_id=order_id, kind=kind, amount=amount))


def split_payout(price: Decimal) -> tuple[Decimal, Decimal]:
    """(выплата исполнителю, комиссия биржи 5%)."""
    commission = money(price * settings.commission_rate)
    return money(price - commission), commission
