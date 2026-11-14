"""Оценка стоимости запросов к Claude API по токенам.

Цены в долларах за 1 млн токенов (ввод, вывод). Точные списания смотри
на platform.claude.com → Usage; здесь — оценка для команды /cost.
"""

PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
}
DEFAULT_PRICE = PRICES["claude-opus-5-5"]


def price_for(model: str) -> tuple[float, float]:
    for name, price in PRICES.items():
        if model.startswith(name):
            return price
    return DEFAULT_PRICE


def usage_cost(model: str, usage) -> tuple[int, int, float]:
    """Возвращает (входные токены, выходные токены, стоимость $) по объекту usage из ответа."""
    price_in, price_out = price_for(model)
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (inp * price_in + cache_write * price_in * 1.25 + cache_read * price_in * 0.1
            + out * price_out) / 1_000_000
    return inp + cache_write + cache_read, out, cost
