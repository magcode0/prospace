from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
import pandas as pd
from sqlalchemy import func, select

from app.models import Offer, OfferStatus, Order, Role, Transaction, TxKind, utcnow
from app.services import billing, stats
from app.services.notifications import Kind, commit_and_push, notify
from app.web import TZ, CurrentUser, Session, day_label, flash, redirect, render

router = APIRouter(prefix="/balance")

TX_LABELS = {
    TxKind.topup: "Пополнение",
    TxKind.withdraw: "Вывод средств",
    TxKind.placement_fee: "Плата за размещение",
    TxKind.placement_refund: "Возврат платы за размещение",
    TxKind.escrow_hold: "Резерв суммы заказа",
    TxKind.escrow_refund: "Возврат суммы заказа",
    TxKind.response_fee: "Плата за отклик",
    TxKind.response_refund: "Возврат платы за отклик",
    TxKind.payout: "Оплата за заказ (−5% комиссия)",
    TxKind.commission: "Комиссия биржи",
    TxKind.adjustment: "Корректировка поддержкой",
}
MAX_TOPUP = Decimal("1000000")


FILTERS = {"all": "Все", "in": "Поступления", "out": "Списания"}
MONTHS_SHORT = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def _flows(rows: list[tuple], months: int = 6) -> dict:
    """Поступления и списания по месяцам для HTML-графика: столбики в % от верха оси + деления."""
    df = pd.DataFrame(rows, columns=["created_at", "amount"])
    end = pd.Timestamp.now(tz=TZ).tz_localize(None).to_period("M")
    index = pd.period_range(end=end, periods=months, freq="M")
    if df.empty:
        table = pd.DataFrame({"inc": 0.0, "out": 0.0}, index=index)
    else:
        df["amount"] = df["amount"].astype(float)
        df["month"] = pd.to_datetime(df["created_at"], utc=True).dt.tz_convert(TZ).dt.tz_localize(None).dt.to_period("M")
        df["inc"] = df["amount"].clip(lower=0)
        df["out"] = (-df["amount"]).clip(lower=0)
        table = df.groupby("month")[["inc", "out"]].sum().reindex(index, fill_value=0.0)
    step = stats.nice_step(max(table["inc"].max(), table["out"].max()))
    top = step * 4
    return {
        "ticks": [stats.short_money(step * i) for i in range(4, -1, -1)],
        "months": [
            {
                "label": MONTHS_SHORT[p.month - 1],
                "inc": r.inc, "out": r.out,
                "inc_h": round(r.inc / top * 100, 1), "out_h": round(r.out / top * 100, 1),
            }
            for p, r in table.iterrows()
        ],
    }


@router.get("")
async def balance_page(request: Request, session: Session, user: CurrentUser, f: str = "all"):
    f = f if f in FILTERS else "all"
    stmt = select(Transaction).where(Transaction.user_id == user.id)
    if f == "in":
        stmt = stmt.where(Transaction.amount > 0)
    elif f == "out":
        stmt = stmt.where(Transaction.amount < 0)
    rows = (await session.execute(stmt.order_by(Transaction.created_at.desc()).limit(150))).scalars().all()

    all_rows = (
        await session.execute(select(Transaction.created_at, Transaction.amount).where(Transaction.user_id == user.id))
    ).all()
    flows = _flows(all_rows)

    if user.role == Role.client:
        reserved = await session.scalar(select(func.coalesce(func.sum(Order.escrow), 0)).where(Order.client_id == user.id))
        reserved_note = "сумма активных заказов на безопасной сделке"
    else:
        reserved = await session.scalar(
            select(func.coalesce(func.sum(Offer.fee_paid), 0)).where(
                Offer.freelancer_id == user.id, Offer.status == OfferStatus.responded
            )
        )
        reserved_note = "плата за отклики — вернётся, если вас не выберут"

    today = utcnow().astimezone(TZ).date()
    groups: list[tuple[str, list[Transaction]]] = []
    for t in rows:
        label = day_label(t.created_at.astimezone(TZ).date(), today)
        if not groups or groups[-1][0] != label:
            groups.append((label, []))
        groups[-1][1].append(t)

    return await render(
        request, session, "balance.html", user,
        groups=groups, labels=TX_LABELS, filters=FILTERS, current=f, flows=flows,
        month_in=flows["months"][-1]["inc"], month_out=flows["months"][-1]["out"], reserved=reserved, reserved_note=reserved_note,
    )


@router.get("/export.csv")
async def export_csv(session: Session, user: CurrentUser):
    """Выписка по счёту в CSV (pandas), открывается в Excel."""
    rows = (
        await session.execute(
            select(Transaction.created_at, Transaction.kind, Transaction.order_id, Transaction.amount)
            .where(Transaction.user_id == user.id)
            .order_by(Transaction.created_at.desc())
        )
    ).all()
    df = pd.DataFrame(rows, columns=["Дата", "Операция", "Заказ", "Сумма, ₽"])
    if not df.empty:
        df["Дата"] = pd.to_datetime(df["Дата"], utc=True).dt.tz_convert(TZ).dt.strftime("%d.%m.%Y %H:%M")
        df["Операция"] = df["Операция"].map(lambda k: TX_LABELS[TxKind(k)])
        df["Сумма, ₽"] = df["Сумма, ₽"].astype(float)
    csv = df.to_csv(index=False, sep=";", decimal=",")
    return Response(
        "\ufeff" + csv,  # BOM — чтобы Excel понял UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="prospace-statement.csv"'},
    )


def _parse_amount(raw: str) -> Decimal | None:
    try:
        amount = billing.money(Decimal(raw.replace(" ", "").replace(",", ".")))
    except InvalidOperation:
        return None
    return amount if Decimal("1") <= amount <= MAX_TOPUP else None


@router.post("/topup")
async def topup(request: Request, session: Session, user: CurrentUser, amount: Annotated[str, Form()] = ""):
    """Демо-пополнение. В продакшене здесь редирект на платёжный шлюз и зачисление по его webhook."""
    value = _parse_amount(amount)
    if value is None:
        flash(request, "Сумма — от 1 до 1 000 000 ₽", "error")
        return redirect("/balance")
    locked = await billing.lock_user(session, user.id)
    await billing.credit(session, locked, value, TxKind.topup)
    notify(session, locked, Kind.finance, f"Баланс пополнен на {value:.0f} ₽", "/balance")
    await commit_and_push(session)
    flash(request, f"Баланс пополнен на {value:.0f} ₽", "success")
    return redirect("/balance")


@router.post("/withdraw")
async def withdraw(request: Request, session: Session, user: CurrentUser, amount: Annotated[str, Form()] = ""):
    value = _parse_amount(amount)
    if value is None:
        flash(request, "Некорректная сумма", "error")
        return redirect("/balance")
    locked = await billing.lock_user(session, user.id)
    try:
        await billing.charge(session, locked, value, TxKind.withdraw)
    except billing.InsufficientFunds as exc:
        await session.rollback()
        flash(request, str(exc), "error")
        return redirect("/balance")
    await session.commit()
    flash(request, f"Заявка на вывод {value:.0f} ₽ принята", "success")
    return redirect("/balance")


@router.get("/chart.png")
async def chart(session: Session, user: CurrentUser):
    income = (TxKind.payout,)
    spend = (TxKind.escrow_hold, TxKind.placement_fee, TxKind.response_fee)
    kinds, title = (income, "Заработок по месяцам") if user.role.value == "freelancer" else (spend, "Расходы по месяцам")
    rows = (
        await session.execute(
            select(Transaction.created_at, Transaction.amount).where(Transaction.user_id == user.id, Transaction.kind.in_(kinds))
        )
    ).all()
    df = stats.monthly([(t, abs(a)) for t, a in rows])
    png = await run_in_threadpool(stats.bar_chart_monthly, df, title)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})
