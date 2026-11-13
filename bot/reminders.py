"""Логика напоминаний: расписание на день, откладывание, отметка «поел».

Правила:
- Каждый день создаются напоминания по списку времени (по умолчанию 12:00, 17:00, 21:00).
- «Отложить» сдвигает текущее напоминание на N минут (по умолчанию 120)
  и на столько же сдвигает все следующие напоминания этого дня:
  12:00 -> 14:00, тогда 17:00 -> 19:00, 21:00 -> 23:00.
- Напоминания не приходят позже LATEST_TIME: такие помечаются пропущенными.
- «Поел» отмечает напоминание выполненным.
- Если ты записал еду, бот сам закрывает последнее присланное напоминание
  и не присылает то, до которого осталось меньше AUTO_DONE_WINDOW.
- Если бот был выключен и напоминание опоздало больше чем на STALE_AFTER, оно не присылается.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .db import DB

LATEST_TIME = time(23, 30)
AUTO_DONE_WINDOW = timedelta(minutes=60)
STALE_AFTER = timedelta(minutes=60)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse_times(raw: list[str]) -> list[time]:
    out = []
    for t in raw:
        h, m = t.strip().split(":")
        out.append(time(int(h), int(m)))
    return sorted(out)


def ensure_day(db: DB, day: date, tz: ZoneInfo) -> None:
    """Создаёт напоминания на день, если их ещё нет."""
    if db.reminders_for_day(day):
        return
    for slot, t in enumerate(parse_times(db.reminder_times())):
        db.insert_reminder(day, slot, datetime.combine(day, t, tzinfo=tz))


def reset_day(db: DB, day: date, tz: ZoneInfo, now: datetime) -> None:
    """Пересоздаёт оставшиеся напоминания дня после смены расписания."""
    db.conn.execute("DELETE FROM reminders WHERE day = ?", (day.isoformat(),))
    db.conn.commit()
    ensure_day(db, day, tz)
    for r in db.reminders_for_day(day):
        if _dt(r["due_at"]) <= now:
            db.update_reminder(day, r["slot"], status="missed")


def due_reminders(db: DB, now: datetime) -> list[int]:
    """Номера напоминаний, которые пора отправить сейчас (статус сразу меняется на sent)."""
    day = now.date()
    result = []
    for r in db.reminders_for_day(day):
        if r["status"] != "pending":
            continue
        due = _dt(r["due_at"])
        if due > now:
            continue
        if now - due > STALE_AFTER:
            db.update_reminder(day, r["slot"], status="missed")
            continue
        db.update_reminder(day, r["slot"], status="sent")
        result.append(r["slot"])
    return result


def snooze(db: DB, day: date, slot: int, now: datetime, minutes: int) -> datetime | None:
    """Откладывает напоминание и сдвигает следующие. Возвращает новое время или None."""
    delta = timedelta(minutes=minutes)
    latest = datetime.combine(day, LATEST_TIME, tzinfo=now.tzinfo)
    new_due = None
    for r in db.reminders_for_day(day):
        if r["slot"] == slot:
            due = now + delta
            if due > latest:
                db.update_reminder(day, slot, status="skipped")
                continue
            db.update_reminder(day, slot, due_at=due.isoformat(), status="pending",
                               snoozes=r["snoozes"] + 1)
            new_due = due
        elif r["slot"] > slot and r["status"] == "pending":
            due = _dt(r["due_at"]) + delta
            if due > latest:
                db.update_reminder(day, r["slot"], status="skipped")
            else:
                db.update_reminder(day, r["slot"], due_at=due.isoformat())
    return new_due


def mark_done(db: DB, day: date, slot: int) -> None:
    db.update_reminder(day, slot, status="done")


def on_meal_logged(db: DB, now: datetime) -> None:
    """Закрывает напоминания, которые уже не нужны, раз еда записана."""
    day = now.date()
    for r in db.reminders_for_day(day):
        due = _dt(r["due_at"])
        if r["status"] == "sent":
            db.update_reminder(day, r["slot"], status="done")
        elif r["status"] == "pending" and due - now <= AUTO_DONE_WINDOW:
            db.update_reminder(day, r["slot"], status="done")


def describe_day(db: DB, day: date) -> str:
    icons = {"pending": "⏳", "sent": "🔔", "done": "✅", "skipped": "⏭", "missed": "❌"}
    lines = []
    for r in db.reminders_for_day(day):
        t = _dt(r["due_at"]).strftime("%H:%M")
        extra = f" (отложено ×{r['snoozes']})" if r["snoozes"] else ""
        lines.append(f"{icons.get(r['status'], '?')} {t}{extra}")
    return "\n".join(lines) or "Напоминаний нет."


def meals_done_count(db: DB, day: date) -> tuple[int, int]:
    rows = db.reminders_for_day(day)
    return sum(1 for r in rows if r["status"] == "done"), len(rows)
