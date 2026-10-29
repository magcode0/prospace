"""Отчёты на pandas + графики matplotlib (PNG) в строгом стиле биржи."""

import io
import math
from collections.abc import Sequence

import pandas as pd
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

# Figure вместо pyplot: графики рендерятся в пуле потоков, а pyplot хранит глобальное состояние.

INK = "#12152A"
MUTED = "#5A5F7A"
GRID = "#E4E6EE"
ACCENT = "#3D5AFE"
SURFACE = "#ffffff"

STATUS_LABELS = {
    "moderation": "Модерация",
    "rejected": "Отклонён модерацией",
    "open": "Подбор",
    "in_progress": "В работе",
    "submitted": "На проверке",
    "revision": "Правки",
    "declined": "Окно спора",
    "disputed": "Спор",
    "completed": "Выполнен",
    "cancelled": "Снят",
}


def monthly(rows: Sequence[tuple], months: int = 12) -> pd.DataFrame:
    """rows: (created_at, amount) → суммы по месяцам за последние N месяцев (пустые месяцы = 0)."""
    df = pd.DataFrame(rows, columns=["created_at", "amount"])
    end = pd.Timestamp.now(tz="UTC").to_period("M")
    index = pd.period_range(end=end, periods=months, freq="M")
    if df.empty:
        return pd.DataFrame({"amount": 0.0}, index=index)
    df["amount"] = df["amount"].astype(float)
    df["month"] = pd.to_datetime(df["created_at"], utc=True).dt.tz_localize(None).dt.to_period("M")
    return df.groupby("month")["amount"].sum().reindex(index, fill_value=0.0).to_frame()


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(axis="y" if ax.name == "rectilinear" else "x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=144, bbox_inches="tight", facecolor=SURFACE)
    return buf.getvalue()


def bar_chart_monthly(df: pd.DataFrame, title: str) -> bytes:
    fig = Figure(figsize=(8, 3.2))
    ax = fig.subplots()
    labels = [p.strftime("%m.%y") for p in df.index]
    values = df["amount"].tolist()
    bars = ax.bar(labels, values, color=ACCENT, width=0.6)
    _style(ax)
    ax.grid(axis="x", visible=False)
    ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="bold", pad=12)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f} ₽".replace(",", " ")))
    # подписываем только последний месяц — без чисел на каждом столбце
    if values and values[-1]:
        last = bars.patches[-1]
        ax.annotate(f"{values[-1]:,.0f} ₽".replace(",", " "), (last.get_x() + last.get_width() / 2, last.get_height()),
                    xytext=(0, 3), textcoords="offset points", ha="center", color=INK, fontsize=9)
    return _png(fig)


def status_breakdown(counts: dict[str, int], title: str) -> bytes:
    s = pd.Series({STATUS_LABELS.get(k, k): v for k, v in counts.items() if v}).sort_values()
    fig = Figure(figsize=(8, max(1.6, 0.42 * len(s) + 0.8)))
    ax = fig.subplots()
    if s.empty:
        ax.text(0.5, 0.5, "Пока нет заказов", ha="center", va="center", color=MUTED, transform=ax.transAxes)
        ax.axis("off")
    else:
        bars = ax.barh(s.index, s.values, color=ACCENT, height=0.6)
        ax.bar_label(bars, color=INK, fontsize=9, padding=4)
        _style(ax)
        ax.grid(axis="y", visible=False)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.xaxis.get_major_locator().set_params(integer=True)
    ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="bold", pad=12)
    return _png(fig)


def nice_step(top: float, ticks: int = 4) -> float:
    """Шаг оси из ряда 1, 2, 2.5, 5 × 10ⁿ, чтобы подписи были круглыми."""
    raw = max(top, 1) / ticks
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def short_money(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:g} млн".replace(".", ",")
    if v >= 1000:
        return f"{v / 1000:g} тыс".replace(".", ",")
    return f"{v:g}"
