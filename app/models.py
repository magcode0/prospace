import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum(cls: type[enum.Enum]) -> Enum:
    return Enum(cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


Money = Numeric(12, 2)


class Role(str, enum.Enum):
    client = "client"
    freelancer = "freelancer"


class BusyStatus(str, enum.Enum):
    free = "free"
    busy = "busy"


class OrderStatus(str, enum.Enum):
    moderation = "moderation"          # на модерации
    rejected = "rejected"              # не прошёл модерацию
    open = "open"                      # идёт подбор / приём откликов
    in_progress = "in_progress"        # исполнитель назначен
    submitted = "submitted"            # работа отправлена
    revision = "revision"              # отправлен на доработку
    declined = "declined"              # заказчик отклонил работу — окно спора
    disputed = "disputed"              # исполнитель открыл спор
    completed = "completed"            # работа принята, деньги у исполнителя
    cancelled = "cancelled"            # заказ снят, деньги вернулись заказчику


ACTIVE_WORK_STATUSES = (OrderStatus.in_progress, OrderStatus.submitted, OrderStatus.revision)


class OfferStatus(str, enum.Enum):
    sent = "sent"              # предложение отправлено, идёт таймер
    responded = "responded"    # исполнитель откликнулся
    declined = "declined"      # исполнитель отказался
    expired = "expired"        # молчание 2 часа = отказ
    rejected = "rejected"      # заказчик отказал
    accepted = "accepted"      # заказчик утвердил
    closed = "closed"          # подбор остановлен / заказ снят


SLOT_STATUSES = (OfferStatus.sent, OfferStatus.responded)


class TxKind(str, enum.Enum):
    topup = "topup"
    withdraw = "withdraw"
    placement_fee = "placement_fee"
    placement_refund = "placement_refund"
    escrow_hold = "escrow_hold"
    escrow_refund = "escrow_refund"
    response_fee = "response_fee"
    response_refund = "response_refund"
    payout = "payout"
    commission = "commission"
    adjustment = "adjustment"  # ручная корректировка сотрудником


class DisputeStatus(str, enum.Enum):
    open = "open"
    resolved_client = "resolved_client"
    resolved_freelancer = "resolved_freelancer"


class TicketStatus(str, enum.Enum):
    open = "open"
    answered = "answered"
    closed = "closed"


user_categories = Table(
    "user_categories",
    Base.metadata,
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("category_id", ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True),
)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(64))
    last_name: Mapped[str] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(_enum(Role))
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    blocked_reason: Mapped[str | None] = mapped_column(String(300))

    avatar: Mapped[str | None] = mapped_column(String(255))
    about: Mapped[str] = mapped_column(String(200), default="")
    skills: Mapped[str] = mapped_column(String(500), default="")
    busy_status: Mapped[BusyStatus] = mapped_column(_enum(BusyStatus), default=BusyStatus.free)
    min_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("1000"))
    work_hours_start: Mapped[int] = mapped_column(Integer, default=9)
    work_hours_end: Mapped[int] = mapped_column(Integer, default=21)
    email_notifications: Mapped[bool] = mapped_column(Boolean, default=True)

    balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    # статистика для ранжирования
    offers_received: Mapped[int] = mapped_column(Integer, default=0)
    offers_reacted: Mapped[int] = mapped_column(Integer, default=0)
    completed_orders: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[float] = mapped_column(Float, default=0.0)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)

    # суточный лимит откликов
    response_credits: Mapped[int] = mapped_column(Integer, default=5)
    credits_refreshed_on: Mapped[date] = mapped_column(Date, server_default=func.current_date())

    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    categories: Mapped[list[Category]] = relationship(secondary=user_categories, lazy="selectin")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def is_newbie(self) -> bool:
        from app.config import settings

        return self.completed_orders < settings.newbie_completed_threshold


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    freelancer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))

    title: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text)
    skills: Mapped[str] = mapped_column(String(500), default="")
    price: Mapped[Decimal] = mapped_column(Money)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus), default=OrderStatus.moderation, index=True)
    moderation_note: Mapped[str | None] = mapped_column(Text)
    escrow: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    placement_fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    matching_active: Mapped[bool] = mapped_column(Boolean, default=False)
    selection_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispute_window_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    client: Mapped[User] = relationship(foreign_keys=[client_id], lazy="joined")
    freelancer: Mapped[User | None] = relationship(foreign_keys=[freelancer_id], lazy="joined")
    category: Mapped[Category] = relationship(lazy="joined")


class Offer(Base):
    """Предложение заказа исполнителю (слот подбора) и его отклик."""

    __tablename__ = "offers"
    __table_args__ = (UniqueConstraint("order_id", "freelancer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    freelancer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[OfferStatus] = mapped_column(_enum(OfferStatus), default=OfferStatus.sent, index=True)
    is_newbie: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(Text)
    fee_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(lazy="joined")
    freelancer: Mapped[User] = relationship(lazy="joined")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)  # None — счёт биржи
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    kind: Mapped[TxKind] = mapped_column(_enum(TxKind))
    amount: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(String(500))
    link: Mapped[str | None] = mapped_column(String(255))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ChatMessage(Base):
    """Переписка по заказу: тред = (заказ, исполнитель)."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    freelancer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sender: Mapped[User] = relationship(foreign_keys=[sender_id], lazy="joined")


class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    freelancer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[DisputeStatus] = mapped_column(_enum(DisputeStatus), default=DisputeStatus.open)
    resolution: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(lazy="joined")


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("order_id", "author_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    target_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    author: Mapped[User] = relationship(foreign_keys=[author_id], lazy="joined")


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    subject: Mapped[str] = mapped_column(String(200))
    status: Mapped[TicketStatus] = mapped_column(_enum(TicketStatus), default=TicketStatus.open)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(lazy="joined")


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sender: Mapped[User] = relationship(lazy="joined")


class AdminAction(Base):
    """Журнал действий сотрудников поддержки."""

    __tablename__ = "admin_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str] = mapped_column(String(64))
    details: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    staff: Mapped[User] = relationship(lazy="joined")
