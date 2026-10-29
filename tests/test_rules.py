"""Юнит-тесты правил без БД."""

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.models import User
from app.security import password_problems
from app.services import billing, matching
from app.services.matching import Candidate
from app.services.orders import refresh_credits, return_credit

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)  # 15:00 МСК


def make_user(**kw) -> User:
    defaults = dict(
        rating=4.5, completed_orders=10, offers_received=10, offers_reacted=8,
        last_seen_at=NOW - timedelta(minutes=10), work_hours_start=9, work_hours_end=21,
        response_credits=5, credits_refreshed_on=date(2026, 10, 23),
    )
    defaults.update(kw)
    return User(**defaults)


def cands(n: int, prefix: str) -> list[Candidate]:
    return [Candidate(make_user(username=f"{prefix}{i}"), score=1 - i / 100) for i in range(n)]


def names(chosen):
    return [c.user.username for c in chosen]


def test_password_complexity():
    assert password_problems("Abcdef1!") == []
    assert set(password_problems("abc")) >= {"не короче 8 символов", "заглавная буква", "цифра", "спецсимвол"}


def test_pick_7_plus_3():
    chosen = matching.pick(cands(20, "e"), cands(5, "n"), 0, 0, 10)
    assert sum(n.startswith("e") for n in names(chosen)) == 7
    assert sum(n.startswith("n") for n in names(chosen)) == 3


def test_pick_backfills_from_experienced_when_few_newbies():
    chosen = matching.pick(cands(20, "e"), cands(1, "n"), 0, 0, 10)
    assert len(chosen) == 10 and sum(n.startswith("n") for n in names(chosen)) == 1


def test_pick_backfills_from_newbies_when_few_experienced():
    chosen = matching.pick(cands(4, "e"), cands(10, "n"), 0, 0, 10)
    assert len(chosen) == 10 and sum(n.startswith("e") for n in names(chosen)) == 4


def test_pick_returns_everyone_when_less_than_ten():
    assert len(matching.pick(cands(3, "e"), cands(2, "n"), 0, 0, 10)) == 5


def test_pick_refills_freed_slot_with_same_group():
    # заняты 7 опытных и 2 новичка → свободный слот получает новичок
    chosen = matching.pick(cands(5, "e"), cands(5, "n"), 7, 2, 1)
    assert names(chosen) == ["n0"]


def test_ignoring_offers_lowers_rank():
    rng = random.Random(1)
    active = make_user(offers_received=20, offers_reacted=19)
    ignorer = make_user(offers_received=20, offers_reacted=2)
    assert matching.score(active, NOW, rng) > matching.score(ignorer, NOW, random.Random(1))


def test_work_hours():
    assert matching.in_work_hours(make_user(), NOW)  # 15:00 МСК в 9–21
    assert not matching.in_work_hours(make_user(work_hours_start=18, work_hours_end=23), NOW)
    assert matching.in_work_hours(make_user(work_hours_start=22, work_hours_end=16), NOW)  # через полночь
    assert matching.in_work_hours(make_user(work_hours_start=0, work_hours_end=0), NOW)


def test_daily_credits_accumulate_only_two_days():
    u = make_user(response_credits=3, credits_refreshed_on=date(2026, 10, 20))
    refresh_credits(u, date(2026, 10, 23))
    assert u.response_credits == 10
    u = make_user(response_credits=2, credits_refreshed_on=date(2026, 10, 22))
    refresh_credits(u, date(2026, 10, 23))
    assert u.response_credits == 7
    return_credit(u)
    assert u.response_credits == 8


def test_commission_5_percent():
    assert billing.split_payout(Decimal("5000")) == (Decimal("4750.00"), Decimal("250.00"))
