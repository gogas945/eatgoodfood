"""Хранилище SQLite: приёмы пищи, вес, настройки, напоминания, черновики."""
import json
import sqlite3
from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    description TEXT NOT NULL,
    items_json TEXT NOT NULL,
    kcal REAL NOT NULL,
    protein REAL NOT NULL,
    fat REAL NOT NULL,
    carbs REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS meals_day ON meals(day);

CREATE TABLE IF NOT EXISTS weights (
    day TEXT PRIMARY KEY,
    kg REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    day TEXT NOT NULL,
    slot INTEGER NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL,          -- pending | sent | done | skipped | missed
    snoozes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, slot)
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    description TEXT NOT NULL,
    items_json TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    msg_id INTEGER
);
"""

# Норма по умолчанию: 78 кг, 178 см, 21 год, зал 3-5 раз в неделю, цель набор.
DEFAULT_SETTINGS = {
    "norm_kcal": "3000",
    "norm_protein": "160",
    "norm_fat": "85",
    "norm_carbs": "400",
    "reminder_times": "12:00,17:00,21:00",
    "snooze_minutes": "120",
}


class DB:
    def __init__(self, path: str = "food.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        self.conn.commit()

    # ---------- настройки ----------
    def get(self, key: str) -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def norms(self) -> dict:
        return {k: float(self.get(f"norm_{k}")) for k in ("kcal", "protein", "fat", "carbs")}

    def reminder_times(self) -> list[str]:
        raw = self.get("reminder_times").strip()
        return [t for t in raw.split(",") if t] if raw else []

    # ---------- еда ----------
    def add_meal(self, ts: datetime, description: str, items: list[dict]) -> int:
        totals = sum_items(items)
        cur = self.conn.execute(
            "INSERT INTO meals(ts, day, description, items_json, kcal, protein, fat, carbs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), ts.date().isoformat(), description,
             json.dumps(items, ensure_ascii=False),
             totals["kcal"], totals["protein"], totals["fat"], totals["carbs"]),
        )
        self.conn.commit()
        return cur.lastrowid

    def meals_for_day(self, day: date) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM meals WHERE day = ? ORDER BY ts", (day.isoformat(),)
        ).fetchall()

    def day_totals(self, day: date) -> dict:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(kcal),0) kcal, COALESCE(SUM(protein),0) protein, "
            "COALESCE(SUM(fat),0) fat, COALESCE(SUM(carbs),0) carbs, COUNT(*) n "
            "FROM meals WHERE day = ?",
            (day.isoformat(),),
        ).fetchone()
        return dict(row)

    def delete_last_meal(self) -> sqlite3.Row | None:
        row = self.conn.execute("SELECT * FROM meals ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            self.conn.execute("DELETE FROM meals WHERE id = ?", (row["id"],))
            self.conn.commit()
        return row

    # ---------- вес ----------
    def set_weight(self, day: date, kg: float) -> None:
        self.conn.execute(
            "INSERT INTO weights(day, kg) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET kg = excluded.kg",
            (day.isoformat(), kg),
        )
        self.conn.commit()

    def weights_between(self, start: date, end: date) -> list[tuple[date, float]]:
        rows = self.conn.execute(
            "SELECT day, kg FROM weights WHERE day BETWEEN ? AND ? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [(date.fromisoformat(r["day"]), r["kg"]) for r in rows]

    # ---------- черновики (ждут подтверждения) ----------
    def add_draft(self, description: str, items: list[dict], comment: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO drafts(created, description, items_json, comment) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(), description, json.dumps(items, ensure_ascii=False), comment),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_draft(self, draft_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "description": row["description"],
                "items": json.loads(row["items_json"]), "comment": row["comment"]}

    def set_draft_message(self, draft_id: int, msg_id: int) -> None:
        self.conn.execute("UPDATE drafts SET msg_id = ? WHERE id = ?", (msg_id, draft_id))
        self.conn.commit()

    def draft_by_message(self, msg_id: int) -> dict | None:
        row = self.conn.execute("SELECT id FROM drafts WHERE msg_id = ?", (msg_id,)).fetchone()
        return self.get_draft(row["id"]) if row else None

    def update_draft(self, draft_id: int, description: str, items: list[dict], comment: str) -> None:
        self.conn.execute(
            "UPDATE drafts SET description = ?, items_json = ?, comment = ? WHERE id = ?",
            (description, json.dumps(items, ensure_ascii=False), comment, draft_id),
        )
        self.conn.commit()

    def delete_draft(self, draft_id: int) -> None:
        self.conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
        self.conn.commit()

    # ---------- напоминания ----------
    def reminders_for_day(self, day: date) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE day = ? ORDER BY slot", (day.isoformat(),)
        ).fetchall()

    def insert_reminder(self, day: date, slot: int, due_at: datetime) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO reminders(day, slot, due_at, status) VALUES (?, ?, ?, 'pending')",
            (day.isoformat(), slot, due_at.isoformat()),
        )
        self.conn.commit()

    def update_reminder(self, day: date, slot: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE reminders SET {cols} WHERE day = ? AND slot = ?",
            (*fields.values(), day.isoformat(), slot),
        )
        self.conn.commit()


def sum_items(items: list[dict]) -> dict:
    return {k: round(sum(float(i.get(k, 0)) for i in items), 1)
            for k in ("kcal", "protein", "fat", "carbs")}
