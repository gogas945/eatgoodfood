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


def format_draft(data: dict) -> str:
    items = data["items"]
    lines = ["🍽 <b>Разбор</b>"]
    for i in items:
        lines.append(
            f"• {escape(i['name'])} {fmt(i['grams'])} г: {fmt(i['kcal'])} ккал, "
            f"Б {fmt(i['protein'])} / Ж {fmt(i['fat'])} / У {fmt(i['carbs'])}"
        )
    t = sum_items(items)
    lines.append(
        f"\n<b>Итого: {fmt(t['kcal'])} ккал</b>, Б {fmt(t['protein'])} / "
        f"Ж {fmt(t['fat'])} / У {fmt(t['carbs'])}"
    )
    if data.get("comment"):
        lines.append(f"\n<i>{escape(data['comment'])}</i>")
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


def reminder_text(db: DB, day: date, slot: int, total_slots: int) -> str:
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
