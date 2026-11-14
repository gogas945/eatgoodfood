"""Тексты для Telegram: разбор блюда, итоги дня, недельная сводка, советы «добрать»."""
from datetime import date, timedelta
from html import escape

from .db import DB, sum_items

NAMES = {"kcal": "Калории", "protein": "Белки", "fat": "Жиры", "carbs": "Углеводы"}
UNITS = {"kcal": "ккал", "protein": "г", "fat": "г", "carbs": "г"}
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Калорийные перекусы для добора нормы: (название, ккал, белок г)
SNACKS = [
    ("Арахисовая паста 30 г + банан", 290, 9),
    ("Горсть орехов 40 г", 250, 7),
    ("Творог 5% 200 г + мёд 20 г", 310, 34),
    ("Стакан молока 3,2% + 2 банана", 390, 12),
    ("Гейнер-смузи: молоко 300 мл, банан, овсянка 40 г, арахисовая паста 20 г", 650, 23),
    ("Сырники 3 шт со сметаной", 450, 25),
    ("Бутерброд: хлеб 2 куска, сыр 40 г, ветчина 50 г", 400, 24),
    ("Греческий йогурт 200 г + гранола 50 г", 380, 22),
    ("Протеиновый коктейль на молоке", 280, 32),
    ("Финики 5 шт + кефир 300 мл", 330, 10),
]


def fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}".rstrip("0").rstrip(".")


def bar(value: float, norm: float, width: int = 10) -> str:
    ratio = value / norm if norm else 0
    filled = min(width, round(ratio * width))
    return "▓" * filled + "░" * (width - filled) + f" {ratio * 100:.0f}%"


def items_lines(items: list[dict]) -> list[str]:
    return [
        f"• {escape(i['name'])} {fmt(i['grams'])} г: {fmt(i['kcal'])} ккал, "
        f"Б {fmt(i['protein'])} / Ж {fmt(i['fat'])} / У {fmt(i['carbs'])}"
        for i in items
    ]


def totals_line(t: dict, prefix: str = "Итого") -> str:
    return (f"<b>{prefix}: {fmt(t['kcal'])} ккал</b>, Б {fmt(t['protein'])} / "
            f"Ж {fmt(t['fat'])} / У {fmt(t['carbs'])}")


def label_line(label: dict) -> str:
    return (f"📋 Этикетка: <b>{escape(label['name'])}</b>, на 100 г: {fmt(label['kcal'])} ккал, "
            f"Б {fmt(label['protein'])} / Ж {fmt(label['fat'])} / У {fmt(label['carbs'])}")


def format_draft(data: dict, title: str = "🍽 <b>Разбор</b>") -> str:
    items = data["items"]
    lines = [title]
    if items:
        lines += items_lines(items)
        lines.append("\n" + totals_line(sum_items(items)))
    if data.get("label"):
        lines.append("\n" + label_line(data["label"]))
    if data.get("comment"):
        lines.append(f"\n<i>{escape(data['comment'])}</i>")
    if items:
        lines.append("\nЧтобы поправить, ответь на это сообщение (например: «курица была 250 г»).")
    return "\n".join(lines)


def progress_block(totals: dict, norms: dict) -> str:
    lines = []
    for k in ("kcal", "protein", "fat", "carbs"):
        lines.append(
            f"{NAMES[k]}: {fmt(totals[k])} / {fmt(norms[k])} {UNITS[k]}\n{bar(totals[k], norms[k])}"
        )
    return "\n".join(lines)


def remaining(totals: dict, norms: dict) -> dict:
    return {k: max(0.0, norms[k] - totals[k]) for k in norms}


def today_report(db: DB, day: date) -> str:
    totals = db.day_totals(day)
    norms = db.norms()
    meals = db.meals_for_day(day)
    lines = [f"📊 <b>{WEEKDAYS[day.weekday()]}, {day:%d.%m}</b>\n", progress_block(totals, norms)]
    left = remaining(totals, norms)
    if left["kcal"] > 0:
        lines.append(f"\nОсталось: {fmt(left['kcal'])} ккал, белка {fmt(left['protein'])} г")
    else:
        lines.append("\n✅ Норма по калориям выполнена!")
    if meals:
        lines.append("\n<b>Приёмы пищи:</b>")
        for m in meals:
            lines.append(f"• {m['ts'][11:16]} {escape(m['description'][:60])}: {fmt(m['kcal'])} ккал")
    return "\n".join(lines)


def suggest_snacks(kcal_left: float, protein_left: float, limit: int = 3) -> list[str]:
    """Подбирает перекусы, которые закрывают остаток, с упором на белок, если его не хватает."""
    if kcal_left <= 50:
        return []
    need_protein = protein_left >= 20
    ranked = sorted(
        SNACKS,
        key=lambda s: (-(s[2] / s[1]) if need_protein else 0, abs(s[1] - min(kcal_left, 650))),
    )
    return [f"{name} (~{kcal} ккал, Б {p} г)" for name, kcal, p in ranked[:limit]]


def reminder_text(db: DB, day: date, slot: int, total_slots: int,
                  suggestions: str | None = None) -> str:
    totals = db.day_totals(day)
    norms = db.norms()
    left = remaining(totals, norms)
    is_last = slot == total_slots - 1
    head = "🍴 Время поесть!" if not is_last else "🌙 Последний приём на сегодня"
    lines = [
        head,
        f"Съедено {fmt(totals['kcal'])} из {fmt(norms['kcal'])} ккал "
        f"({totals['kcal'] / norms['kcal'] * 100:.0f}%), белка {fmt(totals['protein'])} г.",
    ]
    if is_last and left["kcal"] > 50:
        if suggestions:
            lines.append("\n" + suggestions)
        else:
            lines.append(f"\nДо нормы не хватает {fmt(left['kcal'])} ккал. Варианты:")
            lines += [f"• {s}" for s in suggest_snacks(left["kcal"], left["protein"])]
    lines.append("\nНапиши, что съел, или нажми кнопку.")
    return "\n".join(lines)


def day_mark(pct: float) -> str:
    if pct >= 95:
        return "✅"
    if pct >= 85:
        return "🟡"
    return "🔴"


def week_report(db: DB, end_day: date) -> str:
    """Сводка за 7 дней, заканчивая end_day включительно."""
    norms = db.norms()
    days = [end_day - timedelta(days=i) for i in range(6, -1, -1)]
    lines = [f"📅 <b>Неделя {days[0]:%d.%m}–{end_day:%d.%m}</b>\n"]
    logged = []
    for d in days:
        t = db.day_totals(d)
        if t["n"] == 0:
            lines.append(f"⚪ {WEEKDAYS[d.weekday()]} {d:%d.%m}: нет записей")
            continue
        logged.append(t)
        pct = t["kcal"] / norms["kcal"] * 100
        diff = t["kcal"] - norms["kcal"]
        sign = "+" if diff >= 0 else "−"
        lines.append(
            f"{day_mark(pct)} {WEEKDAYS[d.weekday()]} {d:%d.%m}: {fmt(t['kcal'])} ккал "
            f"({sign}{fmt(abs(diff))}), Б {fmt(t['protein'])}"
        )

    if not logged:
        lines.append("\nЗа неделю нет записей о еде.")
        return "\n".join(lines)

    n = len(logged)
    avg = {k: sum(t[k] for t in logged) / n for k in norms}
    lines.append(f"\n<b>В среднем за {n} дн. с записями:</b>")
    for k in norms:
        pct = avg[k] / norms[k] * 100
        lines.append(f"{day_mark(pct)} {NAMES[k]}: {fmt(avg[k])} / {fmt(norms[k])} {UNITS[k]} ({pct:.0f}%)")

    hit = sum(1 for t in logged if t["kcal"] >= norms["kcal"] * 0.95)
    lines.append(f"\nНорма калорий выполнена: {hit} из 7 дней")
    worst = min(norms, key=lambda k: avg[k] / norms[k])
    if avg[worst] / norms[worst] < 0.9:
        lines.append(f"Чаще всего не добираешь: <b>{NAMES[worst].lower()}</b>")

    weekly_deficit = sum(max(0, norms["kcal"] - t["kcal"]) for t in logged)
    if weekly_deficit:
        lines.append(f"Суммарный недобор за неделю: {fmt(weekly_deficit)} ккал")

    lines.append(weight_block(db, end_day, avg["kcal"] / norms["kcal"]))
    return "\n".join(lines)


def weight_block(db: DB, end_day: date, kcal_adherence: float) -> str:
    this_week = db.weights_between(end_day - timedelta(days=6), end_day)
    prev_week = db.weights_between(end_day - timedelta(days=13), end_day - timedelta(days=7))
    if not this_week:
        return "\n⚖️ Взвешиваний на этой неделе нет. Взвешивайся утром натощак: /weight 78.4"
    cur = sum(w for _, w in this_week) / len(this_week)
    out = [f"\n⚖️ Средний вес за неделю: {cur:.1f} кг"]
    if not prev_week:
        out.append("Изменение покажу со следующей недели.")
        return "\n".join(out)
    prev = sum(w for _, w in prev_week) / len(prev_week)
    delta = cur - prev
    out.append(f"Изменение к прошлой неделе: {delta:+.2f} кг")
    out.append(norm_advice(delta, kcal_adherence))
    return "\n".join(out)


def norm_advice(weekly_gain: float, kcal_adherence: float) -> str:
    """Совет по норме. Цель для набора: примерно +0,2…0,5 кг в неделю."""
    if kcal_adherence < 0.9:
        return ("💡 Сначала стабильно добирай текущую норму. "
                "Менять её рано, пока она не выполняется.")
    if weekly_gain < 0.15:
        return "💡 Вес почти не растёт при выполненной норме: предлагаю поднять калории на 150–200 ккал (/norm)."
    if weekly_gain > 0.6:
        return "💡 Вес растёт быстрее 0,6 кг в неделю, часть может уходить в жир. Можно снизить норму на 100–150 ккал."
    return "💡 Темп набора хороший, норму оставляем."


# ---------- мои блюда, продукты, холодильник ----------
def dishes_text(dishes: list[dict]) -> str:
    if not dishes:
        return ("📖 <b>Мои блюда</b>\n\nПока пусто. Отправь, что ешь, как обычно "
                "(например, «овсянка 80 г, молоко 250 мл, банан»), и под разбором нажми "
                "<b>⭐ В мои блюда</b>.")
    lines = ["📖 <b>Мои блюда</b>\n"]
    for d in dishes:
        t = d["totals"]
        lines.append(f"• {escape(d['name'])}: {fmt(t['kcal'])} ккал, Б {fmt(t['protein'])}")
    lines.append("\nНажми на блюдо, чтобы записать, изменить или удалить. "
                 "Ещё можно просто написать боту его название.")
    return "\n".join(lines)


def dish_card(dish: dict) -> str:
    lines = [f"⭐ <b>{escape(dish['name'])}</b> (1 порция)\n"]
    lines += items_lines(dish["items"])
    lines.append("\n" + totals_line(dish["totals"]))
    return "\n".join(lines)


def products_text(products: list[dict]) -> str:
    if not products:
        return ("🏷 <b>Мои продукты</b>\n\nПока пусто. Сфотографируй этикетку с таблицей "
                "пищевой ценности и отправь боту: он предложит запомнить продукт. "
                "Потом бот будет считать его по точным цифрам с упаковки.")
    lines = ["🏷 <b>Мои продукты</b> (на 100 г)\n"]
    for p in products:
        fridge = " 🧊" if p["in_fridge"] else ""
        lines.append(f"• {escape(p['name'])}{fridge}: {fmt(p['kcal'])} ккал, Б {fmt(p['protein'])} / "
                     f"Ж {fmt(p['fat'])} / У {fmt(p['carbs'])}")
    lines.append("\n🧊 — есть в холодильнике. Нажми ❌, чтобы удалить продукт.")
    return "\n".join(lines)


def fridge_text(items: list[dict]) -> str:
    if not items:
        return ("🧊 <b>Холодильник пуст</b>\n\nДобавь, что есть дома, через запятую:\n"
                "<code>/fridge творог, бананы, арахисовая паста, молоко</code>\n\n"
                "Тогда /dobor и вечернее напоминание будут предлагать, чем добрать норму, "
                "из того, что у тебя уже куплено.")
    lines = ["🧊 <b>В холодильнике</b>\n"]
    lines += [f"• {escape(p['name'])}" for p in items]
    lines.append("\nДобавить: <code>/fridge молоко, яйца</code>. "
                 "Закончился продукт — нажми на него ниже.")
    return "\n".join(lines)


def suggestions_text(options: list[dict], left: dict) -> str:
    lines = [f"💡 <b>Чем добрать</b>: осталось {fmt(left['kcal'])} ккал, "
             f"белка {fmt(left['protein'])} г\n"]
    for n, o in enumerate(options, 1):
        t = sum_items(o["items"])
        lines.append(f"<b>{n}. {escape(o['title'])}</b>: {fmt(t['kcal'])} ккал, Б {fmt(t['protein'])}")
        lines += ["   " + line for line in items_lines(o["items"])]
        if o.get("why"):
            lines.append(f"   <i>{escape(o['why'])}</i>")
        lines.append("")
    lines.append("Съел один из вариантов? Нажми кнопку с его номером.")
    return "\n".join(lines)


# ---------- расходы на Claude ----------
def money(v: float) -> str:
    return f"${v:.2f}" if v >= 0.1 else f"{v * 100:.1f} ¢"


def cost_report(db: DB, today: date) -> str:
    day = db.usage_since(today.isoformat())
    month = db.usage_since(today.replace(day=1).isoformat())
    total = db.usage_since("0000")
    lines = ["💰 <b>Расход на Claude</b> (оценка по токенам)\n",
             f"Сегодня: {money(day['cost'])} ({day['n']} запр.)",
             f"С начала месяца: {money(month['cost'])} ({month['n']} запр.)",
             f"Всего: {money(total['cost'])} ({total['n']} запр.)"]
    if total["n"]:
        lines.append(f"В среднем за запрос: {money(total['cost'] / total['n'])}")
    by_purpose = db.usage_by_purpose(today.replace(day=1).isoformat())
    if by_purpose:
        lines.append("\nЗа месяц по типам:")
        lines += [f"• {escape(k)}: {money(v)}" for k, v in sorted(by_purpose.items(), key=lambda x: -x[1])]
    bal = balance_left(db)
    if bal is not None:
        warn = " ⚠️ пора пополнить" if bal < 1 else ""
        lines.append(f"\nОстаток баланса: <b>≈ ${bal:.2f}</b>{warn}")
    else:
        lines.append("\nЧтобы видеть остаток, укажи баланс из консоли Claude: <code>/cost 5</code>")
    lines.append("Точные цифры: platform.claude.com → Usage.")
    return "\n".join(lines)


def balance_left(db: DB) -> float | None:
    raw = db.get("balance_usd")
    since = db.get("balance_set_at")
    if not raw or not since:
        return None
    return float(raw) - db.usage_since(since)["cost"]
