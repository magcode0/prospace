"""Модерация заказов: цепочка модераторов. Правила работают всегда, ИИ-агент — опционально.

Чтобы подключить другого агента, реализуйте класс с методом `async review(order) -> Verdict`
и добавьте его в build_pipeline().
"""

import logging
import re
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.config import settings
from app.models import Order

log = logging.getLogger(__name__)

Decision = Literal["approve", "reject", "manual"]


class Verdict(BaseModel):
    decision: Decision
    reason: str = Field(description="Короткое пояснение для заказчика на русском языке")


class Moderator(Protocol):
    async def review(self, order: Order) -> Verdict: ...


STOP_WORDS = re.compile(
    r"(курсов\w* работ\w* за меня|сдать экзамен за|накрутк\w*|фейков\w* отзыв\w*|"
    r"взлом\w*|ddos|казино|наркот\w*|поддельн\w* документ\w*)",
    re.IGNORECASE,
)
CONTACTS = re.compile(r"(t\.me/|telegram|whatsapp|@[a-z0-9_]{4,}|\+7\d{10}|8\d{10})", re.IGNORECASE)


class RuleModerator:
    async def review(self, order: Order) -> Verdict:
        text = f"{order.title}\n{order.description}"
        if STOP_WORDS.search(text):
            return Verdict(decision="reject", reason="Заказ нарушает правила биржи.")
        if CONTACTS.search(text):
            return Verdict(decision="reject", reason="Не указывайте контакты вне биржи — общайтесь в чате заказа.")
        if len(order.description.strip()) < 30:
            return Verdict(decision="reject", reason="Описание (ТЗ) слишком короткое — опишите задачу подробнее.")
        return Verdict(decision="approve", reason="Автоматическая проверка пройдена.")


AI_SYSTEM_PROMPT = """Ты модератор фриланс-биржи ProSpace. Проверь заказ заказчика.
Отклоняй (reject): незаконные услуги, мошенничество, накрутки и фейковые отзывы, выполнение
учебных работ и экзаменов за студента, контент для взрослых, попытки увести сделку с биржи
(контакты, внешние платежи), бессмысленный текст.
Отправляй на ручную проверку (manual), если сомневаешься.
Иначе одобряй (approve). Поле reason — одно короткое предложение на русском для заказчика."""


class ClaudeModerator:
    """ИИ-агент модерации на Claude. Включается AI_MODERATION_ENABLED=true + ANTHROPIC_API_KEY."""

    def __init__(self) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=60.0)

    async def review(self, order: Order) -> Verdict:
        anthropic = self._anthropic
        content = (
            f"Название: {order.title}\nКатегория: {order.category.name}\n"
            f"Навыки: {order.skills}\nБюджет: {order.price} ₽\n\nОписание:\n{order.description}"
        )
        try:
            response = await self.client.messages.parse(
                model=settings.ai_moderation_model,
                max_tokens=2048,
                system=AI_SYSTEM_PROMPT,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": content}],
                output_format=Verdict,
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APIStatusError):
            log.exception("ИИ-модерация недоступна, заказ %s уходит на ручную проверку", order.id)
            return Verdict(decision="manual", reason="Заказ проверяет модератор.")
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return Verdict(decision="manual", reason="Заказ проверяет модератор.")
        return response.parsed_output


def build_pipeline() -> list[Moderator]:
    pipeline: list[Moderator] = [RuleModerator()]
    if settings.ai_moderation_enabled and settings.anthropic_api_key:
        pipeline.append(ClaudeModerator())
    return pipeline


async def moderate(order: Order, pipeline: list[Moderator] | None = None) -> Verdict:
    verdict = Verdict(decision="approve", reason="Заказ одобрен.")
    for moderator in pipeline if pipeline is not None else build_pipeline():
        verdict = await moderator.review(order)
        if verdict.decision != "approve":
            return verdict
    return verdict
