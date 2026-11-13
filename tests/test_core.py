import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot import reminders as rem
from bot.db import DB
from bot.nutrition import NutritionParser
from bot.reports import format_draft, norm_advice, suggest_snacks, today_report, week_report

TZ = ZoneInfo("Europe/Moscow")
DAY = date(2026, 11, 16)  # понедельник


def at(h, m=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


@pytest.fixture
def db():
    return DB(":memory:")


def due_times(db):
    return {r["slot"]: (datetime.fromisoformat(r["due_at"]).strftime("%H:%M"), r["status"])
            for r in db.reminders_for_day(DAY)}


def test_reminders_fire_on_time(db):
    rem.ensure_day(db, DAY, TZ)
    assert rem.due_reminders(db, at(11, 59)) == []
    assert rem.due_reminders(db, at(12, 0)) == [0]
    assert rem.due_reminders(db, at(12, 1)) == []  # не дублируется


def test_snooze_shifts_following(db):
    # Пример из задачи: отложил 12:00 на 2 часа -> 14:00, следующее 17:00 -> 19:00
    rem.ensure_day(db, DAY, TZ)
    rem.due_reminders(db, at(12))
    new = rem.snooze(db, DAY, 0, at(12), 120)
    assert new.strftime("%H:%M") == "14:00"
    t = due_times(db)
    assert t[0] == ("14:00", "pending")
    assert t[1] == ("19:00", "pending")
    assert t[2] == ("23:00", "pending")
    assert rem.due_reminders(db, at(14)) == [0]
    assert rem.due_reminders(db, at(17)) == []
    assert rem.due_reminders(db, at(19)) == [1]


def test_snooze_past_latest_is_skipped(db):
    rem.ensure_day(db, DAY, TZ)
    rem.due_reminders(db, at(12))
    rem.snooze(db, DAY, 0, at(12), 120)
    rem.due_reminders(db, at(14))
    rem.snooze(db, DAY, 0, at(14), 120)  # 21:00 -> 23:00 -> 01:00 = поздно
    t = due_times(db)
    assert t[1] == ("21:00", "pending")
    assert t[2][1] == "skipped"


def test_stale_reminders_not_sent_after_restart(db):
    rem.ensure_day(db, DAY, TZ)
    assert rem.due_reminders(db, at(16, 0)) == []
    assert due_times(db)[0][1] == "missed"


def test_meal_logged_closes_reminders(db):
    rem.ensure_day(db, DAY, TZ)
    rem.due_reminders(db, at(12))
    rem.on_meal_logged(db, at(12, 20))
    assert due_times(db)[0][1] == "done"
    # поел в 16:10 — напоминание в 17:00 уже не нужно
    rem.on_meal_logged(db, at(16, 10))
    assert due_times(db)[1][1] == "done"
    assert due_times(db)[2][1] == "pending"


def test_reset_day_with_new_times(db):
    rem.ensure_day(db, DAY, TZ)
    db.set("reminder_times", "10:00,14:00,18:00,22:00")
    rem.reset_day(db, DAY, TZ, at(13))
    t = due_times(db)
    assert len(t) == 4 and t[0][1] == "missed" and t[1] == ("14:00", "pending")


ITEMS = [
    {"name": "Гречка варёная", "grams": 200, "kcal": 220, "protein": 8, "fat": 2, "carbs": 42},
    {"name": "Куриное бедро", "grams": 180, "kcal": 333, "protein": 34, "fat": 21, "carbs": 0},
]


def test_day_totals_and_reports(db):
    db.add_meal(at(13), "обед", ITEMS)
    t = db.day_totals(DAY)
    assert t["kcal"] == 553 and t["protein"] == 42
    text = today_report(db, DAY)
    assert "553 / 3000" in text
    assert "Осталось: 2447" in text
    assert "<b>Итого: 553 ккал</b>" in format_draft({"items": ITEMS, "comment": "a<b"})


def test_week_report(db):
    for i in range(7):
        d = DAY - timedelta(days=6 - i)
        db.add_meal(at(13, day=d), "еда", [{"name": "x", "grams": 1, "kcal": 2400 + i * 100,
                                             "protein": 120, "fat": 80, "carbs": 350}])
        db.set_weight(d, 78.0 + i * 0.01)
        db.set_weight(d - timedelta(days=7), 78.0)
    text = week_report(db, DAY)
    assert "Норма калорий выполнена: 2 из 7" in text
    assert "белки" in text
    assert "Средний вес" in text


def test_norm_advice():
    assert "Сначала" in norm_advice(0.0, 0.8)
    assert "поднять" in norm_advice(0.05, 0.98)
    assert "хороший" in norm_advice(0.3, 1.0)
    assert "снизить" in norm_advice(0.8, 1.0)


def test_snacks():
    assert suggest_snacks(30, 0) == []
    assert len(suggest_snacks(600, 40)) == 3


async def test_parser_builds_request(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    parser = NutritionParser("claude-opus-5-5")
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps({"items": ITEMS, "comment": ""}))],
        )

    monkeypatch.setattr(parser.client.beta.messages, "create", fake_create)
    data = await parser.parse("гречка 200 г, бедро 180 г")
    assert data["items"][1]["kcal"] == 333
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["fallbacks"] == "default"
