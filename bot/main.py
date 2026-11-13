"""Telegram-бот для подсчёта КБЖУ. Запуск: python -m bot.main"""
import asyncio
import logging
import re
from html import escape
from datetime import date, datetime, timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from . import reminders as rem
from .config import Config, load_config
from .db import DB
from .nutrition import NutritionError, NutritionParser
from .reports import (format_draft, fmt, reminder_text, remaining, today_report,
                      week_report)

log = logging.getLogger("eatgoodfood")

HELP = """<b>Как пользоваться</b>

Просто напиши, что съел, с весом:
<code>гречка варёная 200 г, куриное бедро 180 г, масло 10 г</code>
Можно рецепт целиком: «на кастрюлю: ..., съел 1/4».
Можно прислать фото еды или этикетки (с подписью точнее).

Бот покажет разбор, а ты нажмёшь «Сохранить». Чтобы поправить,
ответь на сообщение с разбором: «курица была 250 г».

<b>Команды</b>
/today — сколько съедено и сколько осталось
/week — сводка за 7 дней
/weight 78.4 — записать вес (лучше утром натощак)
/norm — показать норму; /norm 3000 160 85 400 — задать (ккал Б Ж У)
/reminders — расписание напоминаний; /reminders 12:00 17:00 21:00 — изменить; /reminders off — выключить
/undo — удалить последнюю запись еды"""


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


def draft_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сохранить", callback_data=f"save:{draft_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel:{draft_id}"),
    ]])


def reminder_keyboard(day: date, slot: int, snooze_min: int) -> InlineKeyboardMarkup:
    hours = snooze_min / 60
    label = f"{hours:g} ч" if snooze_min % 60 == 0 else f"{snooze_min} мин"
    key = f"{day.isoformat()}:{slot}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Поел", callback_data=f"eat:{key}"),
         InlineKeyboardButton(text=f"⏰ Отложить на {label}", callback_data=f"snooze:{key}")],
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"skip:{key}")],
    ])


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

    # ---------- команды ----------
    @dp.message(CommandStart())
    @dp.message(Command("help"))
    async def cmd_help(message: Message):
        await message.answer(HELP)

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
            )
        except NutritionError as e:
            await message.answer(f"⚠️ {e}")
            return
        if not data["items"]:
            await message.answer(escape(data["comment"]) or "Не нашёл в сообщении еды.")
            return
        if previous:
            draft_id = previous["id"]
            db.update_draft(draft_id, previous["description"] + "; " + text, data["items"], data["comment"])
        else:
            description = text or "фото"
            draft_id = db.add_draft(description, data["items"], data["comment"])
        sent = await message.answer(format_draft(data), reply_markup=draft_keyboard(draft_id))
        db.set_draft_message(draft_id, sent.message_id)

    @dp.message(F.photo)
    async def on_photo(message: Message):
        buf = await message.bot.download(message.photo[-1])
        await handle_food(message, message.caption or "", image=buf.read())

    @dp.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message):
        await handle_food(message, message.text)

    # ---------- кнопки ----------
    @dp.callback_query(F.data.startswith("save:"))
    async def cb_save(call: CallbackQuery):
        draft = db.get_draft(int(call.data.split(":")[1]))
        if not draft:
            await call.answer("Уже сохранено или отменено.")
            return
        db.add_meal(now(), draft["description"], draft["items"])
        db.delete_draft(draft["id"])
        rem.on_meal_logged(db, now())
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer("✅ Сохранено. " + short_progress())
        await call.answer()

    @dp.callback_query(F.data.startswith("cancel:"))
    async def cb_cancel(call: CallbackQuery):
        db.delete_draft(int(call.data.split(":")[1]))
        await call.message.edit_reply_markup(reply_markup=None)
        await call.answer("Отменено")

    def parse_slot(data: str) -> tuple[date, int]:
        _, d, s = data.split(":")
        return date.fromisoformat(d), int(s)

    @dp.callback_query(F.data.startswith("eat:"))
    async def cb_eat(call: CallbackQuery):
        day, slot = parse_slot(call.data)
        rem.mark_done(db, day, slot)
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer("👍 Засчитано! Напиши, что съел, и я посчитаю КБЖУ.")
        await call.answer()

    @dp.callback_query(F.data.startswith("snooze:"))
    async def cb_snooze(call: CallbackQuery):
        day, slot = parse_slot(call.data)
        new_due = rem.snooze(db, day, slot, now(), int(db.get("snooze_minutes")))
        await call.message.edit_reply_markup(reply_markup=None)
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
        await call.message.edit_reply_markup(reply_markup=None)
        await call.answer("Пропущено")

    return dp


async def scheduler(bot: Bot, cfg: Config, db: DB) -> None:
    """Раз в 30 секунд проверяет, не пора ли что-то отправить."""
    tz = cfg.timezone
    chat_id = cfg.allowed_user_id
    while True:
        try:
            now = datetime.now(tz)
            today = now.date()
            rem.ensure_day(db, today, tz)
            total = len(db.reminders_for_day(today))
            for slot in rem.due_reminders(db, now):
                await bot.send_message(
                    chat_id, reminder_text(db, today, slot, total),
                    reply_markup=reminder_keyboard(today, slot, int(db.get("snooze_minutes"))),
                )
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
        except Exception:
            log.exception("Ошибка в планировщике")
        await asyncio.sleep(30)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    db = DB(cfg.db_path)
    parser = NutritionParser(cfg.claude_model)
    bot = Bot(cfg.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(cfg, db, parser)
    await bot.set_my_commands([
        BotCommand(command="today", description="Сегодня: съедено и осталось"),
        BotCommand(command="week", description="Сводка за неделю"),
        BotCommand(command="weight", description="Записать вес"),
        BotCommand(command="norm", description="Норма КБЖУ"),
        BotCommand(command="reminders", description="Напоминания"),
        BotCommand(command="undo", description="Удалить последнюю запись"),
        BotCommand(command="help", description="Помощь"),
    ])
    if cfg.allowed_user_id:
        asyncio.create_task(scheduler(bot, cfg, db))
    else:
        log.warning("ALLOWED_USER_ID не задан: напиши боту /start, чтобы узнать свой ID")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
