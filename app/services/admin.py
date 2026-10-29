"""Действия сотрудников поддержки. Каждое пишется в журнал admin_actions."""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAction, Category, Order, OrderStatus, TxKind, User
from app.services import billing, orders
from app.services.notifications import Kind, notify


class AdminError(Exception):
    pass


def log(session: AsyncSession, staff: User, action: str, target: str, details: str = "") -> None:
    session.add(AdminAction(staff_id=staff.id, action=action, target=target, details=details[:500]))


async def adjust_balance(session: AsyncSession, staff: User, user_id: int, amount: Decimal, reason: str) -> User:
    """Ручная корректировка: положительная сумма — начисление, отрицательная — списание."""
    if not reason.strip():
        raise AdminError("Укажите причину корректировки")
    if amount == 0:
        raise AdminError("Сумма не может быть нулевой")
    user = await billing.lock_user(session, user_id)
    if amount > 0:
        await billing.credit(session, user, amount, TxKind.adjustment)
    else:
        try:
            await billing.charge(session, user, -amount, TxKind.adjustment)
        except billing.InsufficientFunds as exc:
            raise AdminError(str(exc)) from None
    sign = "+" if amount > 0 else "−"
    notify(session, user, Kind.finance, f"Корректировка баланса {sign}{abs(amount):.0f} ₽: {reason.strip()}", "/balance")
    log(session, staff, "balance", f"user:{user.id}", f"{sign}{abs(amount):.2f} ₽ · {reason.strip()}")
    return user


def set_blocked(session: AsyncSession, staff: User, user: User, blocked: bool, reason: str = "") -> None:
    if user.id == staff.id:
        raise AdminError("Нельзя заблокировать самого себя")
    user.is_blocked = blocked
    user.blocked_reason = reason.strip()[:300] or None if blocked else None
    log(session, staff, "block" if blocked else "unblock", f"user:{user.id}", reason.strip())


def set_staff(session: AsyncSession, staff: User, user: User, value: bool) -> None:
    if user.id == staff.id and not value:
        raise AdminError("Нельзя снять права сотрудника с самого себя")
    user.is_staff = value
    log(session, staff, "staff_on" if value else "staff_off", f"user:{user.id}")


async def force_cancel(session: AsyncSession, staff: User, order: Order, reason: str) -> None:
    """Снятие заказа поддержкой до назначения исполнителя — с полным возвратом заказчику."""
    if order.status not in (OrderStatus.moderation, OrderStatus.open):
        raise AdminError("Принудительно снять можно только заказ без исполнителя; для остальных — спор")
    await orders.withdraw_order(session, order, order.client)
    notify(session, order.client, Kind.moderation, f"Поддержка сняла заказ «{order.title}»: {reason.strip() or 'нарушение правил'}. Средства возвращены.", f"/client/orders/{order.id}")
    log(session, staff, "order_cancel", f"order:{order.id}", reason.strip())


def add_category(session: AsyncSession, staff: User, slug: str, name: str) -> Category:
    cat = Category(slug=slug.strip().lower(), name=name.strip())
    session.add(cat)
    log(session, staff, "category_add", f"category:{cat.slug}", cat.name)
    return cat
