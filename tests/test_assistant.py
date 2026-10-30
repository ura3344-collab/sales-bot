import asyncio
from types import SimpleNamespace

import assistant
import shop


def test_prompt_knows_catalog_and_rules():
    prompt = assistant.system_prompt()
    assert "Анна" in prompt and "Зв'язатися з менеджером" in prompt
    # Ціни й кольори беруться з products.csv.
    assert "Фартух Latte короткий — 520 грн" in prompt and "Бежевий" in prompt
    assert "лише під замовлення" in prompt


def test_history_is_trimmed_from_the_start():
    history = []
    for i in range(10):
        assistant.remember(history, f"питання {i}", f"відповідь {i}")
    assert len(history) == assistant.HISTORY and history[0] == {"role": "user", "content": "питання 4"}


def fake_client(response):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return response

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))), calls


def test_reply_text_and_refusal(monkeypatch):
    text = SimpleNamespace(type="text", text=" Добрий день! ")
    client, calls = fake_client(SimpleNamespace(stop_reason="end_turn", content=[text]))
    monkeypatch.setattr(assistant, "_client", client)
    history = [{"role": "user", "content": "Привіт"}, {"role": "assistant", "content": "Вітаю!"}]
    assert asyncio.run(assistant.reply(history, "Скільки коштує Latte?")) == "Добрий день!"
    request = calls[0]
    assert request["model"] == assistant.MODEL and request["fallbacks"] == "default"
    assert request["messages"][-1] == {"role": "user", "content": "Скільки коштує Latte?"}
    assert history == [{"role": "user", "content": "Привіт"}, {"role": "assistant", "content": "Вітаю!"}]

    client, _ = fake_client(SimpleNamespace(stop_reason="refusal", content=[]))
    monkeypatch.setattr(assistant, "_client", client)
    assert asyncio.run(assistant.reply([], "?")) is None
