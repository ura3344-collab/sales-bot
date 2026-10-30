"""Віртуальний помічник «Анна»: відповідає клієнтам одразу за допомогою Claude.
Працює, лише якщо задано ANTHROPIC_API_KEY; без ключа бот відповідає за ключовими словами, як раніше."""
import logging
import os

import anthropic

import shop
import texts

MODEL = "claude-opus-5-5"
HISTORY = 12  # скільки останніх повідомлень розмови (клієнта й Анни) пам'ятає Анна

_client: anthropic.AsyncAnthropic | None = None


def enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        # Ключ із форми хостингу часто має зайвий перенос рядка наприкінці.
        _client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", "").strip())
    return _client


def catalog_text(products: dict[str, shop.Product]) -> str:
    """Каталог одним рядком на модель — щоб Анна знала ціни, кольори, розміри й наявність."""
    lines = []
    for category in shop.categories(products):
        lines.append(f"\n{category}:")
        for model, variants in shop.models(products, category).items():
            prices = sorted({v.price for v in variants})
            price = f"{prices[0]} грн" if len(prices) == 1 else f"{prices[0]}–{prices[-1]} грн"
            colors = ", ".join(v.color + (" (під замовлення)" if v.on_request else "" if v.in_stock else " (немає)")
                               for v in variants if v.color)
            details = [price] + ([f"кольори: {colors}"] if colors else [])
            if variants[0].sizes:
                details.append("розміри: " + " ".join(variants[0].sizes))
            if all(v.on_request for v in variants):
                details.append("лише під замовлення через менеджера")
            lines.append(f"- {model} — " + "; ".join(details) + f". {variants[0].description}")
    return "\n".join(lines)


def system_prompt() -> str:
    return texts.ASSISTANT_PROMPT.format(delivery=texts.DELIVERY, contacts=texts.CONTACTS,
                                         catalog=catalog_text(shop.load_products()))


def remember(history: list[dict], question: str, answer: str) -> None:
    """Додає обмін до історії й обрізає її так, щоб вона починалася з повідомлення клієнта."""
    history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
    del history[:max(0, len(history) - HISTORY)]


async def reply(history: list[dict], question: str) -> str | None:
    """Відповідь Анни; None — якщо Claude недоступний або відмовився відповідати (тоді бот відповідає сам)."""
    try:
        response = await client().beta.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
            messages=history + [{"role": "user", "content": question}],
            # Короткі відповіді в чаті не потребують глибоких роздумів — так швидше й дешевше.
            output_config={"effort": "low"},
            # Якщо модель відмовиться відповідати, API сам повторить запит на запасній моделі.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIError:
        logging.exception("Анна не змогла відповісти")
        return None
    if response.stop_reason == "refusal":
        return None
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text or None
