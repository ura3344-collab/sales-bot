"""Наскрізний сценарій: клієнт збирає кошик і оформлює замовлення, менеджер змінює статус.
Telegram підмінено фальшивим транспортом, тому тест не потребує ні токена, ні мережі."""
import asyncio
import json

import pytest
from telegram import Update
from telegram.request import BaseRequest

import bot

CLIENT, MANAGER, STRANGER = 100, 200, 300


class FakeRequest(BaseRequest):
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        name = url.rsplit("/", 1)[1]
        params = request_data.parameters if request_data else {}
        self.calls.append((name, params))
        if name == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Test", "username": "test_bot"}
        elif name in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            result = {"message_id": len(self.calls), "date": 0,
                      "chat": {"id": params.get("chat_id", 1), "type": "private"},
                      "text": params.get("text", "")}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


class Chat:
    """Мінімальний «клієнт Telegram»: надсилає боту оновлення і показує, що бот відповів."""

    def __init__(self, app, request):
        self.app, self.request, self.update_id = app, request, 0

    def _user(self, user_id):
        return {"id": user_id, "is_bot": False, "first_name": f"User{user_id}", "username": f"user{user_id}"}

    async def _send(self, payload):
        self.update_id += 1
        self.request.calls.clear()
        await self.app.process_update(Update.de_json({"update_id": self.update_id, **payload}, self.app.bot))
        return list(self.request.calls)

    async def say(self, user_id, text):
        message = {"message_id": self.update_id + 1, "date": 0, "chat": {"id": user_id, "type": "private"},
                   "from": self._user(user_id), "text": text}
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
        return await self._send({"message": message})

    async def press(self, user_id, data):
        message = {"message_id": 1, "date": 0, "chat": {"id": user_id, "type": "private"},
                   "from": {"id": 1, "is_bot": True, "first_name": "Test"}, "text": "…"}
        return await self._send({"callback_query": {"id": str(self.update_id), "from": self._user(user_id),
                                                    "chat_instance": "x", "data": data, "message": message}})


def texts_to(calls, chat_id):
    return [p["text"] for name, p in calls
            if name in ("sendMessage", "editMessageText") and p.get("chat_id") == chat_id]


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.delenv("MANAGER_ID", raising=False)

    def runner(scenario):
        async def wrapped():
            request = FakeRequest()
            app = bot.build_app("123456:TEST", tmp_path / "flow.db", request=request)
            await app.initialize()
            try:
                await scenario(Chat(app, request), app)
            finally:
                await app.shutdown()
        asyncio.run(wrapped())
    return runner


def test_full_order_flow(run):
    async def scenario(chat, app):
        assert "Вітаю" in texts_to(await chat.say(CLIENT, "/start"), CLIENT)[0]
        assert "Менеджера" not in texts_to(await chat.say(MANAGER, "/manager"), MANAGER)[0]

        catalog = texts_to(await chat.say(CLIENT, bot.BTN_CATALOG), CLIENT)[0]
        assert "Худі оверсайз сіре — 1290 грн" in catalog

        # Товару №4 немає в наявності — у кошик він не потрапляє.
        await chat.press(CLIENT, "add:4")
        await chat.press(CLIENT, "add:1")
        await chat.press(CLIENT, "add:1")
        await chat.press(CLIENT, "add:3")
        cart = texts_to(await chat.press(CLIENT, "dec:1"), CLIENT)[0]
        assert "Разом: 1740 грн" in cart and "Доставка безкоштовна" in cart
        assert "Кепка" not in cart

        assert "звертатися" in texts_to(await chat.press(CLIENT, "checkout"), CLIENT)[0]
        await chat.say(CLIENT, "Ольга Шевченко")
        assert "неповний" in texts_to(await chat.say(CLIENT, "12345"), CLIENT)[0]
        await chat.say(CLIENT, "050 123 45 67")
        summary = texts_to(await chat.say(CLIENT, "Київ, відділення 5"), CLIENT)[0]
        assert "Разом: 1740 грн" in summary and "Ольга Шевченко" in summary

        calls = await chat.say(CLIENT, bot.BTN_CONFIRM)
        assert "Замовлення №1 прийнято" in texts_to(calls, CLIENT)[0]
        to_manager = texts_to(calls, MANAGER)[0]
        assert "Нове замовлення" in to_manager and "050 123 45 67" in to_manager and "@user100" in to_manager

        # Після оформлення кошик порожній, а замовлення видно клієнту.
        assert "порожній" in texts_to(await chat.say(CLIENT, bot.BTN_CART), CLIENT)[0]
        assert "№1" in texts_to(await chat.say(CLIENT, bot.BTN_MY_ORDERS), CLIENT)[0]

        # Сторонній користувач не може керувати замовленнями.
        await chat.press(STRANGER, "st:1:shipped")
        assert app.bot_data["db"].get_order(1)["status"] == "new"
        assert "лише менеджеру" in texts_to(await chat.say(STRANGER, "/orders"), STRANGER)[0]

        calls = await chat.press(MANAGER, "st:1:shipped")
        assert app.bot_data["db"].get_order(1)["status"] == "shipped"
        assert "Відправлено" in texts_to(calls, MANAGER)[0]
        assert "Відправлено" in texts_to(calls, CLIENT)[0]

        assert "№1" in texts_to(await chat.say(MANAGER, "/orders"), MANAGER)[0]
        stats = texts_to(await chat.say(MANAGER, "/stats"), MANAGER)[0]
        assert "Замовлень: 1" in stats and "1740 грн" in stats

    run(scenario)


def test_manager_id_from_env_ignores_whitespace(run, monkeypatch):
    async def scenario(chat, app):
        monkeypatch.setenv("MANAGER_ID", f" {MANAGER}\n")
        assert "Замовлень ще немає" in texts_to(await chat.say(MANAGER, "/orders"), MANAGER)[0]

    run(scenario)


def test_cancel_keeps_cart(run):
    async def scenario(chat, app):
        await chat.press(CLIENT, "add:2")
        await chat.press(CLIENT, "checkout")
        assert "скасовано" in texts_to(await chat.say(CLIENT, bot.BTN_CANCEL), CLIENT)[0]
        assert "Футболка базова чорна" in texts_to(await chat.say(CLIENT, "/cart"), CLIENT)[0]
        assert app.bot_data["db"].recent_orders() == []

    run(scenario)


def test_checkout_with_empty_cart_is_refused(run):
    async def scenario(chat, app):
        calls = await chat.press(CLIENT, "checkout")
        assert [name for name, _ in calls] == ["answerCallbackQuery"]
        # Розмова не почалася: звичайний текст обробляється як питання, а не як ім'я.
        assert "Доставка та оплата" in texts_to(await chat.say(CLIENT, "яка доставка?"), CLIENT)[0]

    run(scenario)
