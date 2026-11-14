"""Запросы к Claude API: разбор еды (текст/фото/этикетка) и подбор вариантов «добрать норму»."""
import base64
import json
from typing import Callable

import anthropic

PARSE_PROMPT = """Ты — нутрициолог-калькулятор. Пользователь присылает, что он съел:
продукты, ингредиенты блюда и их вес. Твоя задача — разбить это на отдельные продукты
и посчитать для каждого калории, белки, жиры и углеводы на указанный вес.

Правила:
- Если продукт или блюдо есть в справочнике пользователя (он придёт в сообщении),
  используй значения оттуда: они точнее. Блюдо из справочника можно разложить на его состав
  или записать одной строкой — как понятнее.
- Иначе используй стандартные справочные значения (таблицы химического состава продуктов, USDA).
- Различай сырой и готовый вес (крупы, макароны, мясо). Если пользователь не уточнил,
  исходи из контекста: «гречка 150 г» в тарелке обычно значит варёная; если в рецепте
  указано «на кастрюлю», чаще всего это сухой вес. Своё допущение запиши в comment.
- Если вес не указан, оцени типичную порцию и обязательно укажи это в comment.
- Если указан рецепт целиком и съеденная доля («рецепт на 4 порции, съел 1»),
  посчитай только съеденную часть.
- Учитывай масло для жарки, соусы и сахар, если они упомянуты.
- Названия продуктов пиши по-русски, коротко.
- grams — съеденный вес в граммах (для жидкостей мл считай как граммы).
- comment — 1–2 коротких предложения о допущениях. Если всё однозначно, оставь пустую строку.
- Если в сообщении нет еды, верни пустой список items и объясни это в comment.

Этикетки:
- Если на фото есть таблица пищевой ценности (этикетка), заполни label: found = true,
  name — короткое название продукта (с брендом и жирностью, если видно), и значения на 100 г
  ровно как напечатано (если на этикетке только «на порцию», пересчитай на 100 г).
- Если пользователь не указал, сколько съел, а на фото только этикетка, верни пустой items
  и напиши в comment, что продукт можно сохранить, а для записи нужен вес.
- Если этикетки нет, label.found = false, остальные поля label = пустая строка и нули."""

SUGGEST_PROMPT = """Ты помогаешь человеку, который набирает мышечную массу, добрать дневную норму КБЖУ.
Тебе дадут остаток до нормы, что у него есть дома, его сохранённые блюда и что он часто ест.

Предложи 3 разных варианта перекуса или приёма пищи, которые закрывают остаток:
- В первую очередь используй продукты из холодильника. Если их не хватает или список пуст,
  опирайся на сохранённые блюда и то, что человек часто ест; в крайнем случае — на простые
  продукты, которые легко купить (тогда отметь это в why).
- Главное — закрыть калории и белок; жиры и углеводы вторичны. Не превышай остаток калорий
  больше чем на 15%. Если остаток большой (больше 900 ккал), вариант может быть полноценным
  приёмом пищи.
- Порции реалистичные, вес в граммах, КБЖУ на этот вес по справочным значениям или
  по данным пользователя.
- title — короткое название варианта (3–6 слов), why — одно короткое предложение.
- Названия по-русски."""

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "grams": {"type": "number"},
        "kcal": {"type": "number"},
        "protein": {"type": "number"},
        "fat": {"type": "number"},
        "carbs": {"type": "number"},
    },
    "required": ["name", "grams", "kcal", "protein", "fat", "carbs"],
    "additionalProperties": False,
}

PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": ITEM_SCHEMA},
        "comment": {"type": "string"},
        "label": {
            "type": "object",
            "properties": {
                "found": {"type": "boolean"},
                "name": {"type": "string"},
                "kcal": {"type": "number"},
                "protein": {"type": "number"},
                "fat": {"type": "number"},
                "carbs": {"type": "number"},
            },
            "required": ["found", "name", "kcal", "protein", "fat", "carbs"],
            "additionalProperties": False,
        },
    },
    "required": ["items", "comment", "label"],
    "additionalProperties": False,
}

SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                    "items": {"type": "array", "items": ITEM_SCHEMA},
                },
                "required": ["title", "why", "items"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["options"],
    "additionalProperties": False,
}

# on_usage(model, purpose, usage) — чтобы записывать расход в базу
UsageCallback = Callable[[str, str, object], None]


class NutritionError(Exception):
    pass


def _clean_items(items: list[dict]) -> list[dict]:
    for item in items:
        for k in ("grams", "kcal", "protein", "fat", "carbs"):
            item[k] = round(max(0.0, float(item[k])), 1)
    return items


def reference_text(products: list[dict], dishes: list[dict]) -> str:
    """Справочник пользователя для промпта."""
    parts = []
    if products:
        lines = [f"- {p['name']}: {p['kcal']:g} ккал, Б {p['protein']:g}, Ж {p['fat']:g}, "
                 f"У {p['carbs']:g}" for p in products[:60]]
        parts.append("Сохранённые продукты (на 100 г):\n" + "\n".join(lines))
    if dishes:
        lines = []
        for d in dishes[:40]:
            comp = ", ".join(f"{i['name']} {i['grams']:g} г" for i in d["items"])
            t = d["totals"]
            lines.append(f"- {d['name']} (1 порция: {t['kcal']:g} ккал, Б {t['protein']:g}, "
                         f"Ж {t['fat']:g}, У {t['carbs']:g}): {comp}")
        parts.append("Сохранённые блюда:\n" + "\n".join(lines))
    return "\n\n".join(parts)


class NutritionParser:
    def __init__(self, model: str, on_usage: UsageCallback | None = None):
        self.client = anthropic.AsyncAnthropic()
        self.model = model
        self.on_usage = on_usage

    async def _ask(self, system: str, content: list[dict], schema: dict, purpose: str) -> dict:
        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
                # Если основная модель откажет, API сам повторит запрос на запасной.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise NutritionError("Неверный ANTHROPIC_API_KEY.") from e
        except anthropic.RateLimitError as e:
            raise NutritionError("Слишком много запросов к Claude, попробуй через минуту.") from e
        except anthropic.APIStatusError as e:
            raise NutritionError(f"Ошибка Claude API ({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise NutritionError("Нет связи с Claude API.") from e

        if self.on_usage is not None:
            try:
                self.on_usage(getattr(response, "model", None) or self.model, purpose,
                              getattr(response, "usage", None))
            except Exception:
                pass  # учёт расходов не должен ломать основную работу

        if response.stop_reason == "refusal":
            raise NutritionError("Claude отказался обработать это сообщение.")
        if response.stop_reason == "max_tokens":
            raise NutritionError("Ответ получился слишком длинным, разбей запрос на части.")
        text_out = next((b.text for b in response.content if b.type == "text"), None)
        if not text_out:
            raise NutritionError("Пустой ответ от Claude.")
        return json.loads(text_out)

    async def parse(
        self,
        text: str,
        image: bytes | None = None,
        image_type: str = "image/jpeg",
        previous: dict | None = None,
        reference: str = "",
    ) -> dict:
        """Возвращает {"items": [...], "comment": str, "label": dict | None}.

        previous — прошлый разбор, если пользователь присылает исправление.
        reference — справочник пользователя (сохранённые продукты и блюда).
        """
        content: list[dict] = []
        if image is not None:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": image_type,
                           "data": base64.standard_b64encode(image).decode()},
            })
        if reference:
            content.append({"type": "text", "text": "Справочник пользователя:\n\n" + reference})
        if previous is not None:
            prompt = (
                "Ранее ты посчитал так:\n"
                f"{json.dumps(previous, ensure_ascii=False)}\n\n"
                f"Пользователь уточняет: {text}\n\n"
                "Верни исправленный полный список."
            )
        elif image is not None:
            prompt = ("Оцени по фото, что это за еда, вес порции и КБЖУ. Если это этикетка, "
                      f"прочитай её. Подпись пользователя: {text or '(нет)'}")
        else:
            prompt = text
        content.append({"type": "text", "text": prompt})

        data = await self._ask(PARSE_PROMPT, content, PARSE_SCHEMA, "разбор еды")
        _clean_items(data["items"])
        label = data.get("label") or {}
        if label.get("found") and label.get("name") and float(label.get("kcal", 0)) > 0:
            data["label"] = {k: (round(float(label[k]), 1) if k != "name" else label[k].strip())
                             for k in ("name", "kcal", "protein", "fat", "carbs")}
        else:
            data["label"] = None
        return data

    async def suggest(self, left: dict, fridge: list[str], frequent: list[str],
                      reference: str) -> list[dict]:
        """Три варианта, чем добрать остаток. left — остаток {kcal, protein, fat, carbs}."""
        lines = [
            f"Осталось до нормы: {left['kcal']:.0f} ккал, белок {left['protein']:.0f} г, "
            f"жиры {left['fat']:.0f} г, углеводы {left['carbs']:.0f} г.",
            "В холодильнике: " + (", ".join(fridge) if fridge else "список не заполнен"),
            "Часто ест: " + (", ".join(frequent) if frequent else "нет данных"),
        ]
        if reference:
            lines.append("\n" + reference)
        data = await self._ask(SUGGEST_PROMPT, [{"type": "text", "text": "\n".join(lines)}],
                               SUGGEST_SCHEMA, "подсказки")
        options = [o for o in data["options"] if o["items"]][:3]
        for o in options:
            _clean_items(o["items"])
        return options
