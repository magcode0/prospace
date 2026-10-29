from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "ProSpace"
    secret_key: str = "change-me-in-production"
    base_url: str = "http://localhost:8000"
    timezone: str = "Europe/Moscow"

    database_url: str = "postgresql+asyncpg://prospace:prospace@localhost:5433/prospace"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"

    # SMTP (в docker-compose — Mailpit, веб-интерфейс на :8025)
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_tls: bool = False
    mail_from: str = "ProSpace <no-reply@prospace.local>"

    media_dir: str = "media"

    # Коды подтверждения
    code_ttl_seconds: int = 15 * 60

    # Финансы
    placement_fee: Decimal = Decimal("100")
    response_fee: Decimal = Decimal("50")
    commission_rate: Decimal = Decimal("0.05")
    min_order_price: Decimal = Decimal("1000")

    # Подбор исполнителей
    match_slots: int = 10
    match_experienced_slots: int = 7
    match_newbie_slots: int = 3
    newbie_completed_threshold: int = 3
    offer_ttl_hours: int = 2
    max_last_seen_hours: int = 4
    order_response_limit: int = 10
    selection_deadline_hours: int = 72
    daily_responses: int = 5
    responses_carry_days: int = 2

    # Сроки
    deadline_reminder_hours: int = 5
    dispute_window_hours: int = 2
    dispute_review_hours: int = 24
    online_threshold_minutes: int = 5

    # ИИ-модерация (опционально)
    ai_moderation_enabled: bool = False
    anthropic_api_key: str | None = None
    ai_moderation_model: str = "claude-opus-5-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
