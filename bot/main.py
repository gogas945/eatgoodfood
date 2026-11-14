"""Telegram-бот для подсчёта КБЖУ. Запуск: python -m bot.main"""
import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from . import reminders as rem
from .config import Config, load_config
from .costs import usage_cost
from .db import DB, sum_items
from .nutrition import NutritionError, NutritionParser, reference_text
from .reports import (balance_left, cost_report, dish_card, dishes_text, fmt, format_draft,
                      fridge_text, products_text, reminder_text, remaining, suggestions_text,
                      today_report, week_report)

log = logging.getLogger("eatgoodfood")

HELP = """<b>Как пользоваться</b>

Просто напиши, что съел, с весом:
<code>гречка варёная 200 г, куриное бедро 180 г, масло 10 г</code>
Можно рецепт целиком: «на кастрюлю: ..., съел 1/4».
Можно прислать фото еды или этикетки (с подписью точнее).

Бот покажет разбор, а ты нажмёшь «Сохранить». Чтобы поправить,
ответь на сообщение с разбором: «курица была 250 г».

<b>Мои блюда</b>
Под разбором нажми «⭐ В мои блюда», и в следующий раз хватит одной кнопки
или просто названия блюда. /dishes — список: записать, изменить, удалить.

<b>Продукты и холодильник</b>
Сфотографируй этикетку, и бот запомнит точное КБЖУ продукта.
/products — мои продукты
/fridge творог, бананы — что есть дома
/dobor — чем добрать норму из того, что есть

<b>Команды</b>
/today — сколько съедено и сколько осталось
/week — сводка за 7 дней
/weight 78.4 — записать вес (лучше утром натощак)
/norm — показать норму; /norm 3000 160 85 400 — задать (ккал Б Ж У)
/reminders — расписание напоминаний; /reminders 12:00 17:00 21:00 — изменить; /reminders off — выключить
/cost — сколько потрачено на Claude; /cost 5 — указать баланс из консоли
/undo — удалить последнюю запись еды
/cancel — отменить ввод (название блюда и т.п.)"""


class Flow(StatesGroup):
    dish_name = State()    # ждём название для нового блюда (data: draft_id)
    dish_edit = State()    # ждём новый состав блюда (data: dish_id)
    dish_rename = State()  # ждём новое название блюда (data: dish_id)


class OwnerOnly(BaseMiddleware):
    """Отвечает только владельцу, чтобы чужие люди не тратили твой API-ключ."""

    def __init__(self, owner_id: int | None):
        self.owner_id = owner_id

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return None
        if self.owner_id is None:
            if isinstance(event, Message):
                await event.answer(
                    f"Твой Telegram ID: <code>{user.id}</code>\n"
                    "Впиши его в файл .env в строку ALLOWED_USER_ID и перезапусти бота."
                )
            return None
        if user.id != self.owner_id:
            return None
        return await handler(event, data)


# ---------- клавиатуры ----------
def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup | None:
    rows = [row for row in rows if row]
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def draft_keyboard(draft: dict) -> InlineKeyboardMarkup:
    d = draft["id"]
    rows = []
    if draft["items"]:
        rows.append([("✅ Сохранить", f"save:{d}"), ("❌ Отмена", f"cancel:{d}")])
        rows.append([("⭐ В мои блюда", f"dishsave:{d}")])
    if draft.get("label"):
        rows.append([("💾 Запомнить продукт", f"prodsave:{d}")])
        if not draft["items"]:
            rows.append([("❌ Закрыть", f"cancel:{d}")])
    return kb(rows)


def dish_update_keyboard(dish_id: int, draft_id: int) -> InlineKeyboardMarkup:
    return kb([[("✅ Обновить блюдо", f"dupd:{dish_id}:{draft_id}"), ("❌ Отмена", f"cancel:{draft_id}")]])


def dishes_keyboard(dishes: list[dict]) -> InlineKeyboardMarkup:
    return kb([[(f"{d['name']} · {fmt(d['totals']['kcal'])} ккал", f"dish:{d['id']}")]
               for d in dishes[:60]])


def dish_keyboard(dish_id: int) -> InlineKeyboardMarkup:
    i = dish_id
    return kb([
        [("🍽 Записать порцию", f"deat:{i}:1")],
        [("½ порции", f"deat:{i}:0.5"), ("×1.5", f"deat:{i}:1.5"), ("×2", f"deat:{i}:2")],
        [("✏️ Состав", f"dedit:{i}"), ("✏️ Название", f"dren:{i}"), ("🗑 Удалить", f"ddel:{i}")],
        [("« Все блюда", "dlist")],
    ])


def reminder_rows(day: date, slot: int, snooze_min: int) -> list[list[tuple[str, str]]]:
    hours = snooze_min / 60
    label = f"{hours:g} ч" if snooze_min % 60 == 0 else f"{snooze_min} мин"
    key = f"{day.isoformat()}:{slot}"
    return [
        [("✅ Поел", f"eat:{key}"), (f"⏰ Отложить на {label}", f"snooze:{key}")],
        [("⏭ Пропустить", f"skip:{key}")],
    ]


def reminder_keyboard(day: date, slot: int, snooze_min: int) -> InlineKeyboardMarkup:
    return kb(reminder_rows(day, slot, snooze_min))


def products_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    return kb([[(f"❌ {p['name']}", f"pdel:{p['id']}")] for p in products[:60]])


def fridge_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    rows = [[(f"✔️ Закончилось: {p['name']}", f"fdel:{p['id']}")] for p in items[:60]]
    if items:
        rows.append([("🧹 Очистить всё", "fclear"), ("💡 Чем добрать", "dobor")])
    return kb(rows)


def scale_items(items: list[dict], k: float) -> list[dict]:
    return [{**i, **{f: round(i[f] * k, 1) for f in ("grams", "kcal", "protein", "fat", "carbs")}}
            for i in items]


def split_list(text: str) -> list[str]:
    return [p.strip(" .") for p in re.split(r"[,;\n]", text) if p.strip(" .")]


async def make_suggestions(db: DB, parser: NutritionParser, today: date
                           ) -> tuple[str, list[list[tuple[str, str]]]] | None:
    """Варианты «чем добрать» от Claude. None, если норма уже выполнена."""
    left = remaining(db.day_totals(today), db.norms())
    if left["kcal"] <= 50:
        return None
    fridge = [p["name"] for p in db.products(fridge=True)]
    frequent = db.frequent_foods(today - timedelta(days=30))
    ref = reference_text(db.products(with_nutrition=True), db.dishes())
    options = await parser.suggest(left, fridge, frequent, ref)
    buttons = []
    for n, o in enumerate(options, 1):
        draft_id = db.add_draft(f"Добор: {o['title']}", o["items"], "")
        buttons.append((f"✅ Съел №{n}", f"save:{draft_id}"))
    return suggestions_text(options, left), [buttons]


def build_dispatcher(cfg: Config, db: DB, parser: NutritionParser) -> Dispatcher:
    dp = Dispatcher()
    dp.message.outer_middleware(OwnerOnly(cfg.allowed_user_id))
    dp.callback_query.outer_middleware(OwnerOnly(cfg.allowed_user_id))
    tz = cfg.timezone

    def now() -> datetime:
        return datetime.now(tz)

    def short_progress() -> str:
        t = db.day_totals(now().date())
        n = db.norms()
        left = remaining(t, n)
        return (f"Сегодня: {fmt(t['kcal'])} / {fmt(n['kcal'])} ккал, "
                f"Б {fmt(t['protein'])} / {fmt(n['protein'])} г. "
                f"Осталось {fmt(left['kcal'])} ккал.")

    def reference() -> str:
        return reference_text(db.products(with_nutrition=True), db.dishes())

    async def edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        try:
            await call.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass  # «message is not modified» и т.п.

    async def drop_markup(call: CallbackQuery) -> None:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

    # ---------- команды ----------
    @dp.message(CommandStart())
    @dp.message(Command("help"))
    async def cmd_help(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(HELP)

    @dp.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Отменил.")

    @dp.message(Command("today"))
    async def cmd_today(message: Message):
        await message.answer(today_report(db, now().date()))

    @dp.message(Command("week"))
    async def cmd_week(message: Message):
        await message.answer(week_report(db, now().date()))

    @dp.message(Command("weight"))
    async def cmd_weight(message: Message, command: CommandObject):
        try:
            kg = float((command.args or "").replace(",", "."))
            if not 30 <= kg <= 250:
                raise ValueError
        except ValueError:
            await message.answer("Напиши так: /weight 78.4")
            return
        db.set_weight(now().date(), kg)
        await message.answer(f"⚖️ Записал: {kg:.1f} кг")

    @dp.message(Command("norm"))
    async def cmd_norm(message: Message, command: CommandObject):
        if command.args:
            parts = command.args.replace(",", ".").split()
            try:
                vals = [float(p) for p in parts]
                if len(vals) != 4:
                    raise ValueError
            except ValueError:
                await message.answer("Формат: /norm ккал белки жиры углеводы\nНапример: /norm 3000 160 85 400")
                return
            for key, v in zip(("kcal", "protein", "fat", "carbs"), vals):
                db.set(f"norm_{key}", str(v))
            check = vals[1] * 4 + vals[2] * 9 + vals[3] * 4
            note = ""
            if abs(check - vals[0]) > 150:
                note = f"\n⚠️ Б×4 + Ж×9 + У×4 = {check:.0f} ккал, это не совпадает с {vals[0]:.0f}."
            await message.answer("Норма обновлена." + note)
        n = db.norms()
        await message.answer(
            f"🎯 <b>Норма в день</b>\nКалории: {fmt(n['kcal'])} ккал\nБелки: {fmt(n['protein'])} г\n"
            f"Жиры: {fmt(n['fat'])} г\nУглеводы: {fmt(n['carbs'])} г"
        )

    @dp.message(Command("reminders"))
    async def cmd_reminders(message: Message, command: CommandObject):
        args = (command.args or "").strip()
        if args:
            if args.lower() in ("off", "выкл"):
                db.set("reminder_times", "")
            else:
                times = re.findall(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", args)
                if not times:
                    await message.answer("Формат: /reminders 12:00 17:00 21:00")
                    return
                db.set("reminder_times", ",".join(f"{int(h):02d}:{m}" for h, m in times))
            rem.reset_day(db, now().date(), tz, now())
        times = db.reminder_times()
        if not times:
            await message.answer("🔕 Напоминания выключены.")
            return
        await message.answer(
            f"⏰ Расписание: {', '.join(times)}\n\n<b>Сегодня:</b>\n{rem.describe_day(db, now().date())}"
        )

    @dp.message(Command("undo"))
    async def cmd_undo(message: Message):
        row = db.delete_last_meal()
        if row:
            await message.answer(f"🗑 Удалил: {escape(row['description'][:80])} ({fmt(row['kcal'])} ккал)")
        else:
            await message.answer("Записей нет.")

    @dp.message(Command("cost"))
    async def cmd_cost(message: Message, command: CommandObject):
        if command.args:
            try:
                bal = float(command.args.replace("$", "").replace(",", ".").strip())
                if bal < 0:
                    raise ValueError
            except ValueError:
                await message.answer("Укажи баланс из консоли Claude числом: /cost 4.75")
                return
            db.set("balance_usd", str(bal))
            db.set("balance_set_at", now().isoformat())
            await message.answer(f"Запомнил баланс ${bal:.2f}. Дальше буду вычитать из него расходы.")
        await message.answer(cost_report(db, now().date()))

    @dp.message(Command("dishes"))
    async def cmd_dishes(message: Message):
        dishes = db.dishes()
        await message.answer(dishes_text(dishes), reply_markup=dishes_keyboard(dishes))

    @dp.message(Command("products"))
    async def cmd_products(message: Message):
        products = db.products(with_nutrition=True)
        await message.answer(products_text(products), reply_markup=products_keyboard(products))

    @dp.message(Command("fridge"))
    async def cmd_fridge(message: Message, command: CommandObject):
        if command.args:
            names = split_list(command.args)
            db.add_to_fridge(names)
            await message.answer(f"🧊 Добавил: {escape(', '.join(names))}")
        items = db.products(fridge=True)
        await message.answer(fridge_text(items), reply_markup=fridge_keyboard(items))

    async def send_suggestions(message: Message):
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        try:
            result = await make_suggestions(db, parser, now().date())
        except NutritionError as e:
            await message.answer(f"⚠️ {e}")
            return
        if result is None:
            await message.answer("✅ Норма на сегодня уже выполнена, добирать нечего.")
            return
        text, rows = result
        await message.answer(text, reply_markup=kb(rows))

    @dp.message(Command("dobor"))
    async def cmd_dobor(message: Message):
        await send_suggestions(message)

    # ---------- ввод в несколько шагов ----------
    @dp.message(Flow.dish_name, F.text & ~F.text.startswith("/"))
    async def on_dish_name(message: Message, state: FSMContext):
        data = await state.get_data()
        await state.clear()
        draft = db.get_draft(data.get("draft_id", 0))
        if not draft:
            await message.answer("Разбор уже сохранён или отменён. Отправь блюдо заново.")
            return
        name = message.text.strip()[:60]
        _, created = db.save_dish(name, draft["items"])
        t = sum_items(draft["items"])
        verb = "Сохранил" if created else "Обновил"
        await message.answer(
            f"⭐ {verb} блюдо «{escape(name)}»: {fmt(t['kcal'])} ккал на порцию.\n"
            f"Теперь можно просто написать «{escape(name)}» или выбрать его в /dishes.\n\n"
            "Разбор выше всё ещё можно сохранить как сегодняшний приём пищи."
        )

    @dp.message(Flow.dish_edit, F.text & ~F.text.startswith("/"))
    async def on_dish_edit(message: Message, state: FSMContext):
        data = await state.get_data()
        await state.clear()
        dish = db.get_dish(data.get("dish_id", 0))
        if not dish:
            await message.answer("Блюдо не найдено.")
            return
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        try:
            parsed = await parser.parse(message.text, previous={"items": dish["items"], "comment": ""},
                                        reference=reference())
        except NutritionError as e:
            await message.answer(f"⚠️ {e}")
            return
        if not parsed["items"]:
            await message.answer(escape(parsed["comment"]) or "Не понял состав, попробуй ещё раз.")
            return
        draft_id = db.add_draft(f"dish:{dish['id']}", parsed["items"], parsed["comment"])
        sent = await message.answer(
            format_draft(parsed, title=f"✏️ <b>Новый состав «{escape(dish['name'])}»</b>"),
            reply_markup=dish_update_keyboard(dish["id"], draft_id),
        )
        db.set_draft_message(draft_id, sent.message_id)

    @dp.message(Flow.dish_rename, F.text & ~F.text.startswith("/"))
    async def on_dish_rename(message: Message, state: FSMContext):
        data = await state.get_data()
        await state.clear()
        name = message.text.strip()[:60]
        if db.rename_dish(data.get("dish_id", 0), name):
            await message.answer(f"✏️ Переименовал в «{escape(name)}».")
        else:
            await message.answer("Блюдо с таким названием уже есть. Попробуй другое: /dishes")

    # ---------- еда ----------
    async def handle_food(message: Message, text: str, image: bytes | None = None):
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        previous = None
        reply = message.reply_to_message
        if reply is not None and image is None:
            previous = db.draft_by_message(reply.message_id)
        try:
            data = await parser.parse(
                text, image=image,
                previous={"items": previous["items"], "comment": previous["comment"]} if previous else None,
                reference=reference(),
            )
        except NutritionError as e:
            await message.answer(f"⚠️ {e}")
            return
        if not data["items"] and not data["label"]:
            await message.answer(escape(data["comment"]) or "Не нашёл в сообщении еды.")
            return
        dish_edit_id = None
        if previous:
            draft_id = previous["id"]
            if previous["description"].startswith("dish:"):
                dish_edit_id = int(previous["description"].split(":")[1])
                description = previous["description"]
            else:
                description = previous["description"] + "; " + text
            db.update_draft(draft_id, description, data["items"], data["comment"], data["label"])
        else:
            draft_id = db.add_draft(text or "фото", data["items"], data["comment"], data["label"])
        if dish_edit_id is not None:
            markup = dish_update_keyboard(dish_edit_id, draft_id)
        else:
            markup = draft_keyboard(db.get_draft(draft_id))
        sent = await message.answer(format_draft(data), reply_markup=markup)
        db.set_draft_message(draft_id, sent.message_id)

    @dp.message(StateFilter(None), F.photo)
    async def on_photo(message: Message):
        buf = await message.bot.download(message.photo[-1])
        await handle_food(message, message.caption or "", image=buf.read())

    @dp.message(StateFilter(None), F.text & ~F.text.startswith("/"))
    async def on_text(message: Message):
        if message.reply_to_message is None:
            dish = db.find_dish(message.text)
            if dish:
                await message.answer(dish_card(dish), reply_markup=dish_keyboard(dish["id"]))
                return
        await handle_food(message, message.text)

    # ---------- кнопки: разбор ----------
    @dp.callback_query(F.data.startswith("save:"))
    async def cb_save(call: CallbackQuery):
        draft = db.get_draft(int(call.data.split(":")[1]))
        if not draft or not draft["items"]:
            await call.answer("Уже сохранено или отменено.")
            return
        db.add_meal(now(), draft["description"], draft["items"])
        db.delete_draft(draft["id"])
        rem.on_meal_logged(db, now())
        await drop_markup(call)
        t = sum_items(draft["items"])
        await call.message.answer(f"✅ Записал {fmt(t['kcal'])} ккал. " + short_progress())
        await call.answer()

    @dp.callback_query(F.data.startswith("cancel:"))
    async def cb_cancel(call: CallbackQuery):
        db.delete_draft(int(call.data.split(":")[1]))
        await drop_markup(call)
        await call.answer("Отменено")

    @dp.callback_query(F.data.startswith("dishsave:"))
    async def cb_dishsave(call: CallbackQuery, state: FSMContext):
        draft_id = int(call.data.split(":")[1])
        if not db.get_draft(draft_id):
            await call.answer("Разбор уже сохранён или отменён.")
            return
        await state.set_state(Flow.dish_name)
        await state.update_data(draft_id=draft_id)
        await call.message.answer(
            "Как назвать блюдо? Например: <i>мой завтрак</i>, <i>гейнер-смузи</i>.\n"
            "Если блюдо с таким названием уже есть, его состав обновится. /cancel — отмена."
        )
        await call.answer()

    @dp.callback_query(F.data.startswith("prodsave:"))
    async def cb_prodsave(call: CallbackQuery):
        draft = db.get_draft(int(call.data.split(":")[1]))
        if not draft or not draft.get("label"):
            await call.answer("Этикетка уже сохранена или разбор отменён.")
            return
        lb = draft["label"]
        db.save_product(lb["name"], lb["kcal"], lb["protein"], lb["fat"], lb["carbs"])
        if draft["items"]:
            db.update_draft(draft["id"], draft["description"], draft["items"], draft["comment"], None)
            try:
                await call.message.edit_reply_markup(reply_markup=draft_keyboard(db.get_draft(draft["id"])))
            except TelegramBadRequest:
                pass
        else:
            db.delete_draft(draft["id"])
            await drop_markup(call)
        await call.message.answer(
            f"💾 Запомнил «{escape(lb['name'])}»: {fmt(lb['kcal'])} ккал на 100 г.\n"
            f"Теперь можно писать, например, «{escape(lb['name'])} 150 г». Все продукты: /products"
        )
        await call.answer()

    # ---------- кнопки: мои блюда ----------
    def parse_id(data: str, pos: int = 1) -> int:
        return int(data.split(":")[pos])

    @dp.callback_query(F.data == "dlist")
    async def cb_dlist(call: CallbackQuery):
        dishes = db.dishes()
        await edit(call, dishes_text(dishes), dishes_keyboard(dishes))
        await call.answer()

    @dp.callback_query(F.data.startswith("dish:"))
    async def cb_dish(call: CallbackQuery):
        dish = db.get_dish(parse_id(call.data))
        if not dish:
            await call.answer("Блюдо удалено.")
            return
        await edit(call, dish_card(dish), dish_keyboard(dish["id"]))
        await call.answer()

    @dp.callback_query(F.data.startswith("deat:"))
    async def cb_deat(call: CallbackQuery):
        _, dish_id, mult = call.data.split(":")
        dish = db.get_dish(int(dish_id))
        if not dish:
            await call.answer("Блюдо удалено.")
            return
        k = float(mult)
        items = scale_items(dish["items"], k)
        description = dish["name"] if k == 1 else f"{dish['name']} ×{k:g}"
        db.add_meal(now(), description, items)
        rem.on_meal_logged(db, now())
        t = sum_items(items)
        await call.message.answer(f"✅ Записал «{escape(description)}»: {fmt(t['kcal'])} ккал. "
                                  + short_progress() + "\nОшибся? /undo")
        await call.answer("Записано")

    @dp.callback_query(F.data.startswith("dedit:"))
    async def cb_dedit(call: CallbackQuery, state: FSMContext):
        dish = db.get_dish(parse_id(call.data))
        if not dish:
            await call.answer("Блюдо удалено.")
            return
        await state.set_state(Flow.dish_edit)
        await state.update_data(dish_id=dish["id"])
        await call.message.answer(
            f"✏️ Что поменять в «{escape(dish['name'])}»?\n"
            "Напиши новый состав целиком или только изменения, например: "
            "<i>курицы 200 г вместо 150</i>, <i>убери масло</i>, <i>добавь банан 120 г</i>.\n/cancel — отмена."
        )
        await call.answer()

    @dp.callback_query(F.data.startswith("dupd:"))
    async def cb_dupd(call: CallbackQuery):
        _, dish_id, draft_id = call.data.split(":")
        draft = db.get_draft(int(draft_id))
        dish = db.get_dish(int(dish_id))
        if not draft or not dish:
            await call.answer("Уже обновлено или отменено.")
            return
        db.update_dish_items(dish["id"], draft["items"])
        db.delete_draft(draft["id"])
        await drop_markup(call)
        dish = db.get_dish(dish["id"])
        await call.message.answer("✅ Блюдо обновлено.\n\n" + dish_card(dish),
                                  reply_markup=dish_keyboard(dish["id"]))
        await call.answer()

    @dp.callback_query(F.data.startswith("dren:"))
    async def cb_dren(call: CallbackQuery, state: FSMContext):
        dish = db.get_dish(parse_id(call.data))
        if not dish:
            await call.answer("Блюдо удалено.")
            return
        await state.set_state(Flow.dish_rename)
        await state.update_data(dish_id=dish["id"])
        await call.message.answer(f"Новое название для «{escape(dish['name'])}»? /cancel — отмена.")
        await call.answer()

    @dp.callback_query(F.data.startswith("ddel:"))
    async def cb_ddel(call: CallbackQuery):
        dish = db.get_dish(parse_id(call.data))
        if not dish:
            await call.answer("Блюдо уже удалено.")
            return
        await edit(call, f"Удалить блюдо «{escape(dish['name'])}»? Записи о еде останутся.",
                   kb([[("🗑 Да, удалить", f"ddelok:{dish['id']}"), ("Нет", f"dish:{dish['id']}")]]))
        await call.answer()

    @dp.callback_query(F.data.startswith("ddelok:"))
    async def cb_ddelok(call: CallbackQuery):
        db.delete_dish(parse_id(call.data))
        dishes = db.dishes()
        await edit(call, "🗑 Удалено.\n\n" + dishes_text(dishes), dishes_keyboard(dishes))
        await call.answer("Удалено")

    # ---------- кнопки: продукты и холодильник ----------
    @dp.callback_query(F.data.startswith("pdel:"))
    async def cb_pdel(call: CallbackQuery):
        db.delete_product(parse_id(call.data))
        products = db.products(with_nutrition=True)
        await edit(call, products_text(products), products_keyboard(products))
        await call.answer("Удалено")

    @dp.callback_query(F.data.startswith("fdel:"))
    async def cb_fdel(call: CallbackQuery):
        db.remove_from_fridge(parse_id(call.data))
        items = db.products(fridge=True)
        await edit(call, fridge_text(items), fridge_keyboard(items))
        await call.answer("Убрал")

    @dp.callback_query(F.data == "fclear")
    async def cb_fclear(call: CallbackQuery):
        db.clear_fridge()
        await edit(call, fridge_text([]), None)
        await call.answer("Очищено")

    @dp.callback_query(F.data == "dobor")
    async def cb_dobor(call: CallbackQuery):
        await call.answer()
        await send_suggestions(call.message)

    # ---------- кнопки: напоминания ----------
    def parse_slot(data: str) -> tuple[date, int]:
        _, d, s = data.split(":")
        return date.fromisoformat(d), int(s)

    @dp.callback_query(F.data.startswith("eat:"))
    async def cb_eat(call: CallbackQuery):
        day, slot = parse_slot(call.data)
        rem.mark_done(db, day, slot)
        await drop_markup(call)
        await call.message.answer("👍 Засчитано! Напиши, что съел, и я посчитаю КБЖУ.")
        await call.answer()

    @dp.callback_query(F.data.startswith("snooze:"))
    async def cb_snooze(call: CallbackQuery):
        day, slot = parse_slot(call.data)
        new_due = rem.snooze(db, day, slot, now(), int(db.get("snooze_minutes")))
        await drop_markup(call)
        if new_due:
            await call.message.answer(
                f"⏰ Напомню в {new_due:%H:%M}. Следующие напоминания тоже сдвинулись:\n"
                + rem.describe_day(db, day)
            )
        else:
            await call.message.answer("Уже слишком поздно откладывать, напоминание пропущено.")
        await call.answer()

    @dp.callback_query(F.data.startswith("skip:"))
    async def cb_skip(call: CallbackQuery):
        day, slot = parse_slot(call.data)
        db.update_reminder(day, slot, status="skipped")
        await drop_markup(call)
        await call.answer("Пропущено")

    return dp


async def scheduler(bot: Bot, cfg: Config, db: DB, parser: NutritionParser) -> None:
    """Раз в 30 секунд проверяет, не пора ли что-то отправить."""
    tz = cfg.timezone
    chat_id = cfg.allowed_user_id
    while True:
        try:
            now = datetime.now(tz)
            today = now.date()
            rem.ensure_day(db, today, tz)
            total = len(db.reminders_for_day(today))
            snooze_min = int(db.get("snooze_minutes"))
            for slot in rem.due_reminders(db, now):
                rows = reminder_rows(today, slot, snooze_min)
                suggestions = None
                left = remaining(db.day_totals(today), db.norms())
                if slot == total - 1 and left["kcal"] > 300:
                    try:
                        result = await make_suggestions(db, parser, today)
                        if result:
                            suggestions, extra = result
                            rows = extra + rows
                    except Exception:
                        log.exception("Не удалось получить подсказки, отправляю обычное напоминание")
                await bot.send_message(chat_id, reminder_text(db, today, slot, total, suggestions),
                                       reply_markup=kb(rows))
            # Итог дня в 23:45, по воскресеньям ещё и недельная сводка.
            if now.hour == 23 and now.minute >= 45 and db.get("last_summary") != today.isoformat():
                db.set("last_summary", today.isoformat())
                done, slots = rem.meals_done_count(db, today)
                text = "🌙 <b>Итог дня</b>\n\n" + today_report(db, today)
                if slots:
                    text += f"\n\nПриёмы по напоминаниям: {done} из {slots}"
                await bot.send_message(chat_id, text)
                if today.weekday() == 6:
                    await bot.send_message(chat_id, week_report(db, today))
            # Предупреждение о малом балансе Claude (один раз на каждое пополнение).
            bal = balance_left(db)
            if bal is not None and bal < 1 and db.get("low_balance_warned") != db.get("balance_set_at"):
                db.set("low_balance_warned", db.get("balance_set_at"))
                await bot.send_message(
                    chat_id,
                    f"⚠️ На балансе Claude осталось примерно ${bal:.2f}. Пополни на platform.claude.com "
                    "→ Billing, потом укажи новый баланс: /cost 10",
                )
        except Exception:
            log.exception("Ошибка в планировщике")
        await asyncio.sleep(30)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    db = DB(cfg.db_path)

    def record_usage(model: str, purpose: str, usage) -> None:
        if usage is None:
            return
        inp, out, cost = usage_cost(model, usage)
        db.add_usage(datetime.now(cfg.timezone), model, purpose, inp, out, cost)

    parser = NutritionParser(cfg.claude_model, on_usage=record_usage)
    bot = Bot(cfg.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(cfg, db, parser)
    await bot.set_my_commands([
        BotCommand(command="today", description="Сегодня: съедено и осталось"),
        BotCommand(command="dishes", description="Мои блюда"),
        BotCommand(command="dobor", description="Чем добрать норму"),
        BotCommand(command="fridge", description="Что есть в холодильнике"),
        BotCommand(command="products", description="Мои продукты (с этикеток)"),
        BotCommand(command="week", description="Сводка за неделю"),
        BotCommand(command="weight", description="Записать вес"),
        BotCommand(command="norm", description="Норма КБЖУ"),
        BotCommand(command="reminders", description="Напоминания"),
        BotCommand(command="cost", description="Расход на Claude"),
        BotCommand(command="undo", description="Удалить последнюю запись"),
        BotCommand(command="help", description="Помощь"),
    ])
    if cfg.allowed_user_id:
        asyncio.create_task(scheduler(bot, cfg, db, parser))
    else:
        log.warning("ALLOWED_USER_ID не задан: напиши боту /start, чтобы узнать свой ID")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
