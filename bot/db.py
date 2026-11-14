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

-- Мои блюда: состав на одну порцию
CREATE TABLE IF NOT EXISTS dishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    items_json TEXT NOT NULL,
    created TEXT NOT NULL
);

-- Продукты: КБЖУ на 100 г (с этикетки) и/или отметка «есть в холодильнике»
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kcal REAL, protein REAL, fat REAL, carbs REAL,
    in_fridge INTEGER NOT NULL DEFAULT 0
);

-- Расход на Claude API
CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost REAL NOT NULL
);
"""

# Колонки, добавленные после первой версии (для уже существующих баз)
MIGRATIONS = {
    "drafts": {"label_json": "TEXT"},
}

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
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
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
    def add_draft(self, description: str, items: list[dict], comment: str,
                  label: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO drafts(created, description, items_json, comment, label_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), description, json.dumps(items, ensure_ascii=False), comment,
             json.dumps(label, ensure_ascii=False) if label else None),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_draft(self, draft_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "description": row["description"],
                "items": json.loads(row["items_json"]), "comment": row["comment"],
                "label": json.loads(row["label_json"]) if row["label_json"] else None}

    def set_draft_message(self, draft_id: int, msg_id: int) -> None:
        self.conn.execute("UPDATE drafts SET msg_id = ? WHERE id = ?", (msg_id, draft_id))
        self.conn.commit()

    def draft_by_message(self, msg_id: int) -> dict | None:
        row = self.conn.execute("SELECT id FROM drafts WHERE msg_id = ?", (msg_id,)).fetchone()
        return self.get_draft(row["id"]) if row else None

    def update_draft(self, draft_id: int, description: str, items: list[dict], comment: str,
                     label: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE drafts SET description = ?, items_json = ?, comment = ?, label_json = ? WHERE id = ?",
            (description, json.dumps(items, ensure_ascii=False), comment,
             json.dumps(label, ensure_ascii=False) if label else None, draft_id),
        )
        self.conn.commit()

    def delete_draft(self, draft_id: int) -> None:
        self.conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
        self.conn.commit()

    # ---------- поиск по названию без учёта регистра (SQLite NOCASE не знает кириллицу) ----------
    def _id_by_name(self, table: str, name: str) -> int | None:
        key = name.strip().casefold()
        for r in self.conn.execute(f"SELECT id, name FROM {table}"):
            if r["name"].strip().casefold() == key:
                return r["id"]
        return None

    # ---------- мои блюда ----------
    def save_dish(self, name: str, items: list[dict]) -> tuple[int, bool]:
        """Сохраняет блюдо (или перезаписывает с тем же названием). Возвращает (id, было_ли_новым)."""
        existing = self._id_by_name("dishes", name)
        data = json.dumps(items, ensure_ascii=False)
        if existing is not None:
            self.conn.execute("UPDATE dishes SET items_json = ? WHERE id = ?", (data, existing))
            self.conn.commit()
            return existing, False
        cur = self.conn.execute(
            "INSERT INTO dishes(name, items_json, created) VALUES (?, ?, ?)",
            (name, data, datetime.now().isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid, True

    def _dish(self, row) -> dict:
        items = json.loads(row["items_json"])
        return {"id": row["id"], "name": row["name"], "items": items, "totals": sum_items(items)}

    def dishes(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM dishes").fetchall()
        return sorted((self._dish(r) for r in rows), key=lambda d: d["name"].casefold())

    def get_dish(self, dish_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM dishes WHERE id = ?", (dish_id,)).fetchone()
        return self._dish(row) if row else None

    def find_dish(self, name: str) -> dict | None:
        dish_id = self._id_by_name("dishes", name)
        return self.get_dish(dish_id) if dish_id is not None else None

    def update_dish_items(self, dish_id: int, items: list[dict]) -> None:
        self.conn.execute("UPDATE dishes SET items_json = ? WHERE id = ?",
                          (json.dumps(items, ensure_ascii=False), dish_id))
        self.conn.commit()

    def rename_dish(self, dish_id: int, name: str) -> bool:
        other = self._id_by_name("dishes", name)
        if other is not None and other != dish_id:
            return False
        try:
            self.conn.execute("UPDATE dishes SET name = ? WHERE id = ?", (name, dish_id))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_dish(self, dish_id: int) -> None:
        self.conn.execute("DELETE FROM dishes WHERE id = ?", (dish_id,))
        self.conn.commit()

    # ---------- продукты и холодильник ----------
    def save_product(self, name: str, kcal: float, protein: float, fat: float, carbs: float) -> None:
        """КБЖУ на 100 г. Отметку «в холодильнике» не трогает."""
        existing = self._id_by_name("products", name)
        if existing is not None:
            self.conn.execute(
                "UPDATE products SET name = ?, kcal = ?, protein = ?, fat = ?, carbs = ? WHERE id = ?",
                (name.strip(), kcal, protein, fat, carbs, existing),
            )
        else:
            self.conn.execute(
                "INSERT INTO products(name, kcal, protein, fat, carbs) VALUES (?, ?, ?, ?, ?)",
                (name.strip(), kcal, protein, fat, carbs),
            )
        self.conn.commit()

    def products(self, with_nutrition: bool = False, fridge: bool = False) -> list[dict]:
        sql = "SELECT * FROM products WHERE 1=1"
        if with_nutrition:
            sql += " AND kcal IS NOT NULL"
        if fridge:
            sql += " AND in_fridge = 1"
        rows = self.conn.execute(sql).fetchall()
        return sorted((dict(r) for r in rows), key=lambda p: p["name"].casefold())

    def get_product(self, product_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return dict(row) if row else None

    def delete_product(self, product_id: int) -> None:
        self.conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        self.conn.commit()

    def add_to_fridge(self, names: list[str]) -> None:
        for name in names:
            existing = self._id_by_name("products", name)
            if existing is not None:
                self.conn.execute("UPDATE products SET in_fridge = 1 WHERE id = ?", (existing,))
            else:
                self.conn.execute("INSERT INTO products(name, in_fridge) VALUES (?, 1)", (name.strip(),))
        self.conn.commit()

    def remove_from_fridge(self, product_id: int) -> None:
        """Убирает из холодильника. Продукт без КБЖУ удаляется совсем."""
        self.conn.execute("UPDATE products SET in_fridge = 0 WHERE id = ?", (product_id,))
        self.conn.execute("DELETE FROM products WHERE id = ? AND kcal IS NULL", (product_id,))
        self.conn.commit()

    def clear_fridge(self) -> None:
        self.conn.execute("UPDATE products SET in_fridge = 0")
        self.conn.execute("DELETE FROM products WHERE kcal IS NULL")
        self.conn.commit()

    def frequent_foods(self, since: date, limit: int = 15) -> list[str]:
        """Названия продуктов, которые чаще всего встречались в записях."""
        counts: dict[str, int] = {}
        rows = self.conn.execute("SELECT items_json FROM meals WHERE day >= ?", (since.isoformat(),))
        for r in rows:
            for item in json.loads(r["items_json"]):
                name = item.get("name", "").strip().lower()
                if name:
                    counts[name] = counts.get(name, 0) + 1
        return [n for n, _ in sorted(counts.items(), key=lambda x: -x[1])[:limit]]

    # ---------- расход на Claude ----------
    def add_usage(self, ts: datetime, model: str, purpose: str,
                  input_tokens: int, output_tokens: int, cost: float) -> None:
        self.conn.execute(
            "INSERT INTO api_usage(ts, day, model, purpose, input_tokens, output_tokens, cost) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), ts.date().isoformat(), model, purpose, input_tokens, output_tokens, cost),
        )
        self.conn.commit()

    def usage_since(self, start: str) -> dict:
        """start — дата ('2026-11-01') или момент времени в ISO-формате."""
        row = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost), 0) cost FROM api_usage WHERE ts >= ?", (start,)
        ).fetchone()
        return {"n": row["n"], "cost": row["cost"]}

    def usage_by_purpose(self, start: str) -> dict[str, float]:
        rows = self.conn.execute(
            "SELECT purpose, SUM(cost) cost FROM api_usage WHERE ts >= ? GROUP BY purpose", (start,)
        ).fetchall()
        return {r["purpose"]: r["cost"] for r in rows}

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
