"""Разбор еды через Claude API: текст или фото -> список продуктов с КБЖУ."""
import base64
import json

import anthropic

SYSTEM_PROMPT = """Ты — нутрициолог-калькулятор. Пользователь присылает, что он съел:
продукты, ингредиенты блюда и их вес. Твоя задача — разбить это на отдельные продукты
и посчитать для каждого калории, белки, жиры и углеводы на указанный вес.

Правила:
- Используй стандартные справочные значения (таблицы химического состава продуктов, USDA).
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
- Если в сообщении нет еды, верни пустой список items и объясни это в comment."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
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
            },
        },
        "comment": {"type": "string"},
    },
    "required": ["items", "comment"],
    "additionalProperties": False,
}


class NutritionError(Exception):
    pass


class NutritionParser:
    def __init__(self, model: str):
        self.client = anthropic.AsyncAnthropic()
        self.model = model

    async def parse(
        self,
        text: str,
        image: bytes | None = None,
        image_type: str = "image/jpeg",
        previous: dict | None = None,
    ) -> dict:
        """Возвращает {"items": [...], "comment": str}.

        previous — прошлый разбор, если пользователь присылает исправление.
        """
        content: list[dict] = []
        if image is not None:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": image_type,
                           "data": base64.standard_b64encode(image).decode()},
            })
        if previous is not None:
            prompt = (
                "Ранее ты посчитал так:\n"
                f"{json.dumps(previous, ensure_ascii=False)}\n\n"
                f"Пользователь уточняет: {text}\n\n"
                "Верни исправленный полный список."
            )
        elif image is not None:
            prompt = ("Оцени по фото, что это за еда, вес порции и КБЖУ. "
                      f"Подпись пользователя: {text or '(нет)'}")
        else:
            prompt = text
        content.append({"type": "text", "text": prompt})

        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
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

        if response.stop_reason == "refusal":
            raise NutritionError("Claude отказался обработать это сообщение.")
        if response.stop_reason == "max_tokens":
            raise NutritionError("Ответ получился слишком длинным, разбей приём пищи на части.")

        text_out = next((b.text for b in response.content if b.type == "text"), None)
        if not text_out:
            raise NutritionError("Пустой ответ от Claude.")
        data = json.loads(text_out)
        for item in data["items"]:
            for k in ("grams", "kcal", "protein", "fat", "carbs"):
                item[k] = round(max(0.0, float(item[k])), 1)
        return data
