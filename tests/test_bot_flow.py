"""Сквозные проверки обработчиков бота: поддельный Telegram и поддельный Claude."""
import itertools
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.config import Config
from bot.costs import usage_cost
from bot.db import DB
from bot.main import build_dispatcher, make_suggestions
from bot.nutrition import NutritionParser
from bot.reports import cost_report, reminder_text

TZ = ZoneInfo("Asia/Tbilisi")
OWNER = 606
USER = User(id=OWNER, is_bot=False, first_name="G")
CHAT = Chat(id=OWNER, type="private")

OATS = [
    {"name": "Овсянка", "grams": 80, "kcal": 300, "protein": 10, "fat": 5, "carbs": 52},
    {"name": "Молоко 2,5%", "grams": 250, "kcal": 130, "protein": 7, "fat": 6, "carbs": 12},
]


class MockSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list = []
        self.ids = itertools.count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=next(self.ids), date=datetime.now(), chat=CHAT, text=method.text)
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            return True
        return True

    async def stream_content(self, *a, **kw):
        raise NotImplementedError

    async def close(self):
        pass

    def texts(self) -> list[str]:
        return [m.text for m in self.sent if isinstance(m, (SendMessage, EditMessageText))]

    def last_markup(self):
        for m in reversed(self.sent):
            if isinstance(m, (SendMessage, EditMessageText, EditMessageReplyMarkup)) and m.reply_markup:
                return m.reply_markup
        return None

    def buttons(self) -> dict[str, str]:
        markup = self.last_markup()
        return {b.text: b.callback_data for row in markup.inline_keyboard for b in row} if markup else {}


class FakeParser:
    def __init__(self):
        self.calls = []
        self.next = None

    async def parse(self, text, image=None, image_type="image/jpeg", previous=None, reference=""):
        self.calls.append({"text": text, "previous": previous, "reference": reference})
        return self.next

    async def suggest(self, left, fridge, frequent, reference):
        self.calls.append({"suggest": True, "left": left, "fridge": fridge})
        return [
            {"title": "Творог с бананом", "why": "Есть дома",
             "items": [{"name": "Творог 5%", "grams": 200, "kcal": 242, "protein": 34, "fat": 10,
                        "carbs": 6}]},
            {"title": "Молоко и банан", "why": "",
             "items": [{"name": "Молоко", "grams": 300, "kcal": 156, "protein": 9, "fat": 7,
                        "carbs": 14}]},
        ]


class Harness:
    def __init__(self):
        self.db = DB(":memory:")
        self.parser = FakeParser()
        self.session = MockSession()
        self.bot = Bot("123456:TEST", session=self.session)
        cfg = Config(telegram_token="x", allowed_user_id=OWNER, timezone=TZ, db_path=":memory:",
                     claude_model="claude-opus-5-5")
        self.dp = build_dispatcher(cfg, self.db, self.parser)
        self.ids = itertools.count(1)

    async def text(self, text: str, reply_to: int | None = None):
        reply = None
        if reply_to is not None:
            reply = Message(message_id=reply_to, date=datetime.now(), chat=CHAT, text="draft")
        msg = Message(message_id=next(self.ids), date=datetime.now(), chat=CHAT, from_user=USER,
                      text=text, reply_to_message=reply)
        await self.dp.feed_update(self.bot, Update(update_id=next(self.ids), message=msg))

    async def press(self, data: str):
        msg = Message(message_id=999, date=datetime.now(), chat=CHAT, text="x")
        cq = CallbackQuery(id=str(next(self.ids)), from_user=USER, chat_instance="c", data=data,
                           message=msg)
        await self.dp.feed_update(self.bot, Update(update_id=next(self.ids), callback_query=cq))


@pytest.fixture
def h():
    return Harness()


def no_label(items, comment=""):
    return {"items": items, "comment": comment, "label": None}


async def test_food_draft_save_and_dish_library(h):
    h.parser.next = no_label(OATS)
    await h.text("овсянка 80 г, молоко 250 мл")
    btn = h.session.buttons()
    assert "✅ Сохранить" in btn and "⭐ В мои блюда" in btn

    # сохранить как блюдо
    await h.press(btn["⭐ В мои блюда"])
    await h.text("Мой завтрак")
    dishes = h.db.dishes()
    assert len(dishes) == 1 and dishes[0]["name"] == "Мой завтрак"
    assert dishes[0]["totals"]["kcal"] == 430

    # разбор всё ещё можно сохранить как приём пищи
    await h.press(btn["✅ Сохранить"])
    assert h.db.day_totals(datetime.now(TZ).date())["kcal"] == 430

    # название блюда сообщением -> карточка без запроса к Claude
    calls = len(h.parser.calls)
    await h.text("мой завтрак")
    assert len(h.parser.calls) == calls
    assert "Мой завтрак" in h.session.texts()[-1]
    btn = h.session.buttons()
    await h.press(btn["×2"])
    assert h.db.day_totals(datetime.now(TZ).date())["kcal"] == 430 + 860


async def test_dish_edit_rename_delete(h):
    dish_id, _ = h.db.save_dish("Смузи", OATS)
    await h.press(f"dish:{dish_id}")
    await h.press(f"dedit:{dish_id}")
    new_items = OATS + [{"name": "Банан", "grams": 120, "kcal": 107, "protein": 1, "fat": 0, "carbs": 27}]
    h.parser.next = no_label(new_items)
    await h.text("добавь банан 120 г")
    assert h.parser.calls[-1]["previous"]["items"] == OATS
    btn = h.session.buttons()
    await h.press(btn["✅ Обновить блюдо"])
    assert h.db.get_dish(dish_id)["totals"]["kcal"] == 537

    await h.press(f"dren:{dish_id}")
    await h.text("Гейнер-смузи")
    assert h.db.get_dish(dish_id)["name"] == "Гейнер-смузи"

    await h.press(f"ddel:{dish_id}")
    await h.press(f"ddelok:{dish_id}")
    assert h.db.dishes() == []


async def test_label_saves_product_and_reference(h):
    label = {"name": "Творог Простоквашино 5%", "kcal": 121, "protein": 17, "fat": 5, "carbs": 1.8}
    h.parser.next = {"items": [], "comment": "Укажи вес, чтобы записать.", "label": label}
    await h.text("этикетка")
    btn = h.session.buttons()
    assert "💾 Запомнить продукт" in btn and "✅ Сохранить" not in btn
    await h.press(btn["💾 Запомнить продукт"])
    products = h.db.products(with_nutrition=True)
    assert products[0]["name"] == "Творог Простоквашино 5%" and products[0]["kcal"] == 121

    h.parser.next = no_label(OATS)
    await h.text("творог 200 г")
    assert "Творог Простоквашино 5%: 121 ккал" in h.parser.calls[-1]["reference"]

    await h.text("/products")
    await h.press(h.session.buttons()["❌ Творог Простоквашино 5%"])
    assert h.db.products(with_nutrition=True) == []


async def test_fridge_and_dobor(h):
    await h.text("/fridge творог, бананы; молоко")
    names = [p["name"] for p in h.db.products(fridge=True)]
    assert names == ["бананы", "молоко", "творог"]
    await h.press(h.session.buttons()["✔️ Закончилось: бананы"])
    assert [p["name"] for p in h.db.products(fridge=True)] == ["молоко", "творог"]

    await h.text("/dobor")
    call = h.parser.calls[-1]
    assert call["suggest"] and call["fridge"] == ["молоко", "творог"] and call["left"]["kcal"] == 3000
    assert "Творог с бананом" in h.session.texts()[-1]
    await h.press(h.session.buttons()["✅ Съел №1"])
    assert h.db.day_totals(datetime.now(TZ).date())["kcal"] == 242

    await h.text("/fridge")
    await h.press("fclear")
    assert h.db.products(fridge=True) == []


async def test_cost_command(h):
    usage = SimpleNamespace(input_tokens=1500, output_tokens=600,
                            cache_creation_input_tokens=0, cache_read_input_tokens=None)
    inp, out, cost = usage_cost("claude-opus-5-5", usage)
    assert (inp, out) == (1500, 600) and cost == pytest.approx(0.018)
    await h.text("/cost 5")
    h.db.add_usage(datetime.now(TZ), "claude-opus-5-5", "разбор еды", inp, out, cost)
    report = cost_report(h.db, datetime.now(TZ).date())
    assert "Сегодня: 1.8 ¢ (1 запр.)" in report
    assert "≈ $4.98" in report


async def test_suggestions_in_last_reminder(h):
    today = datetime.now(TZ).date()
    text, rows = await make_suggestions(h.db, h.parser, today)
    msg = reminder_text(h.db, today, 2, 3, text)
    assert "Чем добрать" in msg and "Творог с бананом" in msg
    assert rows[0][0][0] == "✅ Съел №1"


async def test_state_does_not_swallow_commands(h):
    h.parser.next = no_label(OATS)
    await h.text("овсянка")
    await h.press(h.session.buttons()["⭐ В мои блюда"])
    await h.text("/today")  # команда работает и во время ввода названия
    assert "Калории" in h.session.texts()[-1]
    await h.text("/cancel")
    calls = len(h.parser.calls)
    h.parser.next = no_label(OATS)
    await h.text("гречка 200 г")  # после отмены — снова обычный разбор
    assert len(h.parser.calls) == calls + 1 and h.db.dishes() == []


async def test_stranger_is_ignored(h):
    stranger = User(id=1, is_bot=False, first_name="X")
    msg = Message(message_id=5, date=datetime.now(), chat=Chat(id=1, type="private"),
                  from_user=stranger, text="/today")
    await h.dp.feed_update(h.bot, Update(update_id=77, message=msg))
    assert h.session.sent == []


def test_migration_adds_label_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, "
                 "description TEXT NOT NULL, items_json TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '', "
                 "msg_id INTEGER)")
    conn.execute("INSERT INTO drafts(created, description, items_json) VALUES ('x', 'старый', '[]')")
    conn.commit()
    conn.close()
    db = DB(str(path))
    assert db.get_draft(1)["label"] is None
    d = db.add_draft("новый", OATS, "", {"name": "X", "kcal": 1, "protein": 1, "fat": 1, "carbs": 1})
    assert db.get_draft(d)["label"]["name"] == "X"


async def test_parser_label_and_usage(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    seen = []
    parser = NutritionParser("claude-opus-5-5", on_usage=lambda m, p, u: seen.append((m, p, u.output_tokens)))
    import json

    async def fake_create(**kwargs):
        body = {"items": [], "comment": "", "label": {"found": True, "name": " Кефир 1% ", "kcal": 40,
                                                       "protein": 3, "fat": 1, "carbs": 4}}
        return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=20),
                               content=[SimpleNamespace(type="text", text=json.dumps(body))])

    monkeypatch.setattr(parser.client.beta.messages, "create", fake_create)
    data = await parser.parse("", image=b"img")
    assert data["label"] == {"name": "Кефир 1%", "kcal": 40, "protein": 3, "fat": 1, "carbs": 4}
    assert seen == [("claude-opus-5-5", "разбор еды", 20)]

    async def fake_create_nolabel(**kwargs):
        body = {"items": OATS, "comment": "", "label": {"found": False, "name": "", "kcal": 0,
                                                        "protein": 0, "fat": 0, "carbs": 0}}
        return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", usage=None,
                               content=[SimpleNamespace(type="text", text=json.dumps(body))])

    monkeypatch.setattr(parser.client.beta.messages, "create", fake_create_nolabel)
    assert (await parser.parse("овсянка"))["label"] is None


def test_cyrillic_names_case_insensitive():
    db = DB(":memory:")
    db.save_product("Творог 5%", 121, 17, 5, 1.8)
    db.add_to_fridge(["творог 5%", "Бананы"])
    db.add_to_fridge(["бананы"])
    assert [(p["name"], p["in_fridge"]) for p in db.products()] == [("Бананы", 1), ("Творог 5%", 1)]
    d1, new1 = db.save_dish("Мой завтрак", OATS)
    d2, new2 = db.save_dish("мой ЗАВТРАК", OATS[:1])
    assert d1 == d2 and new1 and not new2
    assert db.find_dish("  МОЙ завтрак ")["totals"]["kcal"] == 300
    d3, _ = db.save_dish("Смузи", OATS)
    assert not db.rename_dish(d3, "мой завтрак") and db.rename_dish(d3, "смузи")
