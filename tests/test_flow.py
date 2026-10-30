"""Наскрізний сценарій: клієнт збирає кошик і оформлює замовлення, менеджер змінює статус.
Telegram підмінено фальшивим транспортом, тому тест не потребує ні токена, ні мережі."""
import asyncio
import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from telegram import Update
from telegram.request import BaseRequest

import bot

CLIENT, MANAGER, STRANGER = 100, 200, 300


class FakeRequest(BaseRequest):
    def __init__(self):
        self.calls = []
        # chat_id -> відповідь Telegram з помилкою, щоб імітувати збої надсилання в конкретний чат.
        self.errors = {}
        # file_id -> вміст файлу, який клієнт нібито надіслав боту.
        self.files = {}

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
        if params.get("chat_id") in self.errors:
            return 400, json.dumps({"ok": False, "error_code": 400, **self.errors[params["chat_id"]]}).encode()
        if name.removesuffix(".bin") in self.files:
            return 200, self.files[name.removesuffix(".bin")]
        if name == "getFile":
            result = {"file_id": params["file_id"], "file_unique_id": "u", "file_path": f"{params['file_id']}.bin"}
        elif name == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Test", "username": "test_bot"}
        elif name in ("sendMessage", "editMessageText", "editMessageReplyMarkup", "sendDocument"):
            result = {"message_id": len(self.calls), "date": 0,
                      "chat": {"id": params.get("chat_id", 1), "type": "private"},
                      "text": params.get("text", "")}
        elif name in ("sendPhoto", "editMessageMedia"):
            result = {"message_id": len(self.calls), "date": 0,
                      "chat": {"id": params.get("chat_id", 1), "type": "private"},
                      "caption": params.get("caption", ""),
                      "photo": [{"file_id": "PHOTO_ID", "file_unique_id": "u", "width": 1, "height": 1}]}
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

    async def say(self, user_id, text, group=None):
        """group — ID групи, якщо повідомлення надіслано в групі, а не в особистому чаті."""
        chat = {"id": group, "type": "group", "title": "Замовлення"} if group else {"id": user_id, "type": "private"}
        message = {"message_id": self.update_id + 1, "date": 0, "chat": chat,
                   "from": self._user(user_id), "text": text}
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
        return await self._send({"message": message})

    async def send_file(self, user_id, file_id, document=False):
        """Клієнт надсилає зображення: стиснуте фото або файлом (document)."""
        message = {"message_id": self.update_id + 1, "date": 0, "chat": {"id": user_id, "type": "private"},
                   "from": self._user(user_id)}
        if document:
            message["document"] = {"file_id": file_id, "file_unique_id": "d", "mime_type": "image/png"}
        else:
            message["photo"] = [{"file_id": file_id, "file_unique_id": "p", "width": 1, "height": 1}]
        return await self._send({"message": message})

    async def press(self, user_id, data, on_photo=False):
        """Натискання inline-кнопки; on_photo — кнопка під фото, а не під текстом."""
        message = {"message_id": 1, "date": 0, "chat": {"id": user_id, "type": "private"},
                   "from": {"id": 1, "is_bot": True, "first_name": "Test"}}
        if on_photo:
            message.update(caption="…", photo=[{"file_id": "OLD", "file_unique_id": "o", "width": 1, "height": 1}])
        else:
            message["text"] = "…"
        return await self._send({"callback_query": {"id": str(self.update_id), "from": self._user(user_id),
                                                    "chat_instance": "x", "data": data, "message": message}})


def texts_to(calls, chat_id):
    return [p["text"] for name, p in calls
            if name in ("sendMessage", "editMessageText") and p.get("chat_id") == chat_id]


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.delenv("MANAGER_ID", raising=False)
    monkeypatch.delenv("ORDERS_CHAT_ID", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(bot.logo, "SPOTS_FILE", Path(__file__).parent / "logo_spots.csv")
    # Тести працюють на власному тестовому каталозі без фото, щоб не залежати від справжнього асортименту.
    monkeypatch.setattr(bot.shop, "PRODUCTS_FILE", Path(__file__).parent / "products.csv")
    monkeypatch.setattr(bot.shop, "PHOTOS_DIR", tmp_path / "no-photos")

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
        assert "Анна" in texts_to(await chat.say(CLIENT, "/start"), CLIENT)[0]
        assert "Менеджера" not in texts_to(await chat.say(MANAGER, "/manager"), MANAGER)[0]

        calls = await chat.say(CLIENT, bot.BTN_CATALOG)
        assert "Оберіть розділ" in texts_to(calls, CLIENT)[0]
        assert "Футболки" in str(calls) and "Кепки" in str(calls)
        calls = await chat.press(CLIENT, "c:1")
        assert "Худі" in texts_to(calls, CLIENT)[0] and "Худі оверсайз — 1290 грн" in str(calls)

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
        assert "спосіб доставки" in texts_to(await chat.say(CLIENT, "050 123 45 67"), CLIENT)[0]
        # Спосіб доставки й оплата обираються лише кнопками.
        assert "кнопкою" in texts_to(await chat.say(CLIENT, "Київ"), CLIENT)[0]
        assert "поштомата" in texts_to(await chat.say(CLIENT, "📦 Нова Пошта — поштомат"), CLIENT)[0]
        assert "оплатити" in texts_to(await chat.say(CLIENT, "Київ, 1234"), CLIENT)[0]
        assert "кнопкою" in texts_to(await chat.say(CLIENT, "готівкою"), CLIENT)[0]
        assert "коментар" in texts_to(await chat.say(CLIENT, "💳 Переказ на картку"), CLIENT)[0]
        summary = texts_to(await chat.say(CLIENT, "Подзвоніть перед відправкою"), CLIENT)[0]
        assert "Разом: 1740 грн" in summary and "Ольга Шевченко" in summary
        assert "Доставка: 📦 Нова Пошта — поштомат, Київ, 1234" in summary
        assert "Оплата: 💳 Переказ на картку" in summary and "Коментар: Подзвоніть перед відправкою" in summary

        calls = await chat.say(CLIENT, bot.BTN_CONFIRM)
        done = texts_to(calls, CLIENT)[0]
        assert "Замовлення №1 прийнято" in done and "Реквізити" in done
        to_manager = texts_to(calls, MANAGER)[0]
        assert "Нове замовлення" in to_manager and "050 123 45 67" in to_manager and "@user100" in to_manager
        assert "Переказ на картку" in to_manager and "Подзвоніть перед відправкою" in to_manager

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


def test_model_card_with_photos_and_colors(run, tmp_path, monkeypatch):
    photos = tmp_path / "photos"
    photos.mkdir()
    for pid in ("1", "2"):
        (photos / f"{pid}.jpg").write_bytes(b"fake jpeg")
    monkeypatch.setattr(bot.shop, "PHOTOS_DIR", photos)

    async def scenario(chat, app):
        # Модель відкривається карткою з фото першого кольору і кнопками всіх кольорів.
        calls = await chat.press(CLIENT, "m:1")
        card = next(p for name, p in calls if name == "sendPhoto")
        assert card["caption"] == "Футболка базова — 450 грн\nКолір: Біла\n\n100% бавовна, розміри S–XXL"
        assert "✓ Біла" in str(card["reply_markup"]) and "Чорна" in str(card["reply_markup"])

        # Інший колір — та сама картка з новим фото, підписом і позначкою ✓.
        calls = await chat.press(CLIENT, "v:2:", on_photo=True)
        edit = next(p for name, p in calls if name == "editMessageMedia")
        assert "Колір: Чорна" in str(edit["media"]) and "✓ Чорна" in str(edit["reply_markup"])
        # Фото має завантажуватися файлом: посилання file:// справжній Telegram відхиляє.
        assert "attach://" in str(edit["media"]) and "file://" not in str(edit["media"])

        # Удруге фото не завантажується, а йде за file_id від Telegram.
        calls = await chat.press(CLIENT, "m:1")
        assert [p["photo"] for name, p in calls if name == "sendPhoto"] == ["PHOTO_ID"]

        # Товар без фото показується текстовою карткою; у моделі з одним кольором кнопок кольорів немає.
        calls = await chat.press(CLIENT, "m:3")
        assert "Худі оверсайз — 1290 грн\nКолір: Сіре" in texts_to(calls, CLIENT)[0]
        assert "v:" not in str(calls)

        # Кнопка на картці показує, скільки цього кольору вже в кошику, а кошик — який саме колір.
        calls = await chat.press(CLIENT, "add:2", on_photo=True)
        markup = next(p["reply_markup"] for name, p in calls if name == "editMessageReplyMarkup")
        assert "у кошику: 1" in str(markup)
        assert "Футболка базова (чорна)" in texts_to(await chat.say(CLIENT, "/cart"), CLIENT)[0]

        # Застаріла кнопка після зміни каталогу не ламає бота.
        calls = await chat.press(CLIENT, "c:9")
        assert "Каталог оновився" in str(calls)

    run(scenario)


def test_sizes_chart_and_request_to_manager(run, tmp_path, monkeypatch):
    photos = tmp_path / "photos"
    (photos / "charts").mkdir(parents=True)
    (photos / "charts" / "rio.jpg").write_bytes(b"fake jpeg")
    (photos / "5.jpg").write_bytes(b"fake jpeg")
    monkeypatch.setattr(bot.shop, "PHOTOS_DIR", photos)
    monkeypatch.setenv("MANAGER_ID", str(MANAGER))

    def answer(calls):
        return next(p.get("text", "") for name, p in calls if name == "answerCallbackQuery")

    async def scenario(chat, app):
        card = next(p for name, p in await chat.press(CLIENT, "m:5") if name == "sendPhoto")
        assert "Оберіть розмір" in card["caption"]
        markup = str(card["reply_markup"])
        assert "XS 🕓" in markup and "s:5:M" in markup and "chart:5" in markup and "Інші кольори 🕓" in markup

        # Без розміру одяг у кошик не додається.
        assert "Спочатку оберіть розмір" in answer(await chat.press(CLIENT, "add:5", on_photo=True))

        calls = await chat.press(CLIENT, "s:5:M", on_photo=True)
        edit = next(p for name, p in calls if name == "editMessageCaption")
        assert "Розмір: M" in edit["caption"] and "✓ M" in str(edit["reply_markup"])
        await chat.press(CLIENT, "add:5:M", on_photo=True)
        assert "Кітель Rio (білий, M)" in texts_to(await chat.say(CLIENT, "/cart"), CLIENT)[0]

        # Розмір під замовлення: замість «Додати» — запит менеджеру.
        edit = next(p for name, p in await chat.press(CLIENT, "s:5:XS", on_photo=True) if name == "editMessageCaption")
        assert "Під замовлення" in edit["caption"]
        assert "ask:5:XS" in str(edit["reply_markup"]) and "add:" not in str(edit["reply_markup"])
        calls = await chat.press(CLIENT, "ask:5:XS", on_photo=True)
        to_manager = texts_to(calls, MANAGER)[0]
        assert "Кітель Rio (білий, XS)" in to_manager and f"tg://user?id={CLIENT}" in to_manager
        assert "Запит надіслано" in texts_to(calls, CLIENT)[0]

        # Колір під замовлення: розмір зберігається, купити можна лише через менеджера.
        edit = next(p for name, p in await chat.press(CLIENT, "v:6:M", on_photo=True) if name == "editMessageCaption")
        assert "Інші кольори" in edit["caption"] and "Розмір: M" in edit["caption"]
        assert "ask:6:M" in str(edit["reply_markup"])
        assert "немає в наявності" in answer(await chat.press(CLIENT, "add:6:M", on_photo=True))
        assert "Інші кольори" not in texts_to(await chat.say(CLIENT, "/cart"), CLIENT)[0]

        calls = await chat.press(CLIENT, "chart:5", on_photo=True)
        assert [p["caption"] for name, p in calls if name == "sendPhoto"] == ["📏 Розмірна сітка: Кітель Rio"]

    run(scenario)


def test_write_manager_button(run, monkeypatch):
    async def scenario(chat, app):
        # Без посилання кнопки немає, є звичайне меню.
        monkeypatch.setattr(bot.texts, "MANAGER_LINK", "")
        calls = await chat.say(CLIENT, bot.BTN_CONTACTS)
        assert "t.me" not in str(calls)

        monkeypatch.setattr(bot.texts, "MANAGER_LINK", "https://t.me/shop_manager")
        calls = await chat.say(CLIENT, bot.BTN_CONTACTS)
        assert "Написати менеджеру" in str(calls) and "https://t.me/shop_manager" in str(calls)
        calls = await chat.press(CLIENT, "ask:4:")
        assert "https://t.me/shop_manager" in str(calls)

    run(scenario)


def test_manager_id_from_env_ignores_whitespace(run, monkeypatch):
    async def scenario(chat, app):
        monkeypatch.setenv("MANAGER_ID", f" {MANAGER}\n")
        assert "Замовлень ще немає" in texts_to(await chat.say(MANAGER, "/orders"), MANAGER)[0]

    run(scenario)


async def place_order(chat, client=CLIENT):
    """Новий клієнт швидко оформлює замовлення; повертає виклики бота після підтвердження."""
    await chat.press(client, "add:1")
    await chat.press(client, "checkout")
    for answer in ("Ольга", "050 123 45 67", "📦 Нова Пошта — поштомат", "Київ, 1234",
                   bot.texts.PAYMENT_METHODS[0], bot.BTN_SKIP):
        await chat.say(client, answer)
    return await chat.say(client, bot.BTN_CONFIRM)


def test_orders_go_to_separate_group(run, monkeypatch):
    group = -1001

    async def scenario(chat, app):
        monkeypatch.setenv("MANAGER_ID", str(MANAGER))
        # Призначити групу може лише менеджер і лише в групі.
        assert "лише менеджеру" in texts_to(await chat.say(STRANGER, "/orderschat", group=group), group)[0]
        assert "в групі" in texts_to(await chat.say(MANAGER, "/orderschat"), MANAGER)[0]
        assert f"ORDERS_CHAT_ID = {group}" in texts_to(await chat.say(MANAGER, "/orderschat", group=group), group)[0]

        calls = await place_order(chat)
        assert "Нове замовлення" in texts_to(calls, group)[0]
        assert not texts_to(calls, MANAGER)

        # Статус можна змінити кнопкою прямо в групі.
        await chat._send({"callback_query": {
            "id": "g", "from": chat._user(MANAGER), "chat_instance": "x", "data": "st:1:shipped",
            "message": {"message_id": 1, "date": 0, "text": "…", "chat": {"id": group, "type": "group", "title": "Замовлення"}}}})
        assert app.bot_data["db"].get_order(1)["status"] == "shipped"

    run(scenario)


def test_orders_follow_group_upgraded_to_supergroup(run, monkeypatch):
    old, new = -1001, -1009

    async def scenario(chat, app):
        # ORDERS_CHAT_ID на хостингу ще зі старим номером групи.
        monkeypatch.setenv("MANAGER_ID", str(MANAGER))
        monkeypatch.setenv("ORDERS_CHAT_ID", str(old))
        chat.request.errors[old] = {"description": "Bad Request: group chat was upgraded to a supergroup chat",
                                    "parameters": {"migrate_to_chat_id": new}}
        calls = await place_order(chat)
        assert "Нове замовлення" in texts_to(calls, new)[0]
        assert f"ORDERS_CHAT_ID = {new}" in texts_to(calls, MANAGER)[0]

        # Наступне замовлення одразу йде на новий номер, а кнопки статусу в новій групі працюють.
        calls = await place_order(chat, client=CLIENT + 1)
        assert "Нове замовлення" in texts_to(calls, new)[0] and not texts_to(calls, old)
        await chat._send({"callback_query": {
            "id": "g", "from": chat._user(MANAGER), "chat_instance": "x", "data": "st:2:shipped",
            "message": {"message_id": 1, "date": 0, "text": "…", "chat": {"id": new, "type": "supergroup", "title": "Замовлення"}}}})
        assert app.bot_data["db"].get_order(2)["status"] == "shipped"

    run(scenario)


def test_order_reaches_manager_when_group_fails(run, monkeypatch):
    group = -1001

    async def scenario(chat, app):
        monkeypatch.setenv("MANAGER_ID", str(MANAGER))
        monkeypatch.setenv("ORDERS_CHAT_ID", str(group))
        chat.request.errors[group] = {"description": "Forbidden: bot was kicked from the group chat"}
        to_manager = texts_to(await place_order(chat), MANAGER)[0]
        assert "Не вдалося надіслати замовлення в групу" in to_manager and "Нове замовлення" in to_manager

    run(scenario)


def jpeg(color, size=(100, 150), draw=None):
    img = Image.new("RGB", size, color)
    if draw:
        draw(ImageDraw.Draw(img))
    out = io.BytesIO()
    img.save(out, "JPEG")
    return out.getvalue()


def captions(calls, chat_id, method="sendPhoto"):
    return [p.get("caption") or str(p.get("media")) for name, p in calls
            if name == method and p.get("chat_id") == chat_id]


def test_logo_preview_and_request(run, tmp_path, monkeypatch):
    photos = tmp_path / "photos"
    photos.mkdir()
    for pid, color in (("1", "white"), ("2", "black"), ("5", "white")):
        (photos / f"{pid}.jpg").write_bytes(jpeg(color))
    monkeypatch.setattr(bot.shop, "PHOTOS_DIR", photos)

    async def scenario(chat, app):
        monkeypatch.setenv("MANAGER_ID", str(MANAGER))
        chat.request.files["LOGO"] = jpeg("white", (200, 100), lambda d: d.rectangle((40, 30, 160, 70), fill="black"))
        chat.request.files["BAD"] = b"not an image"

        # Меню показує лише вироби з розміченими фото; на картці товару є кнопка приміряти логотип.
        models = texts_to(await chat.say(CLIENT, bot.BTN_LOGO), CLIENT)
        assert "Оберіть виріб" in models[0]
        calls = await chat.say(CLIENT, bot.BTN_LOGO)
        assert "Футболка базова" in str(calls) and "Кітель Rio" in str(calls) and "Худі" not in str(calls)
        assert "lg:1" in str(await chat.press(CLIENT, "m:1"))

        # Спершу виріб, потім логотип — і бот одразу показує візуалізацію.
        assert "Надішліть логотип" in texts_to(await chat.press(CLIENT, "lg:2"), CLIENT)[0]
        calls = await chat.send_file(CLIENT, "LOGO")
        preview = captions(calls, CLIENT)[0]
        assert "Футболка базова (чорна)" in preview and "10 см, груди" in preview
        # Чорний логотип на чорній тканині перефарбовується, щоб його було видно.
        assert "перефарбовано" in preview
        markup = str(next(p["reply_markup"] for name, p in calls if name == "sendPhoto"))
        assert "✓ 10 см" in markup and "✓ Груди" in markup and "Низ фартуха" in markup and "✓ Чорна" in markup

        # Розмір, місце й колір змінюють ту саму картинку.
        assert "25 см, груди" in captions(await chat.press(CLIENT, "lgs:25", on_photo=True), CLIENT, "editMessageMedia")[0]
        assert "25 см, низ фартуха" in captions(await chat.press(CLIENT, "lgw:bottom", on_photo=True), CLIENT,
                                                "editMessageMedia")[0]
        white = captions(await chat.press(CLIENT, "lgc:1", on_photo=True), CLIENT, "editMessageMedia")[0]
        assert "Футболка базова (біла)" in white and "перефарбовано" not in white

        # Кітель: низу фартуха немає, тож місце й розмір підлаштовуються (на грудях — до 10 см).
        kitel = captions(await chat.press(CLIENT, "lg:5"), CLIENT)[0]
        assert "Кітель Rio (білий)" in kitel and "10 см, груди праворуч" in kitel

        # Запит менеджеру: візуалізація з параметрами і оригінал логотипа.
        calls = await chat.press(CLIENT, "lgo", on_photo=True)
        to_manager = captions(calls, MANAGER)
        assert "Запит на нанесення логотипа" in to_manager[0] and "10 см, груди праворуч" in to_manager[0]
        assert to_manager[1] == "Оригінал логотипа"
        assert [p["photo"] for name, p in calls if name == "sendPhoto"] == ["OLD", "LOGO"]
        assert "Менеджер отримав візуалізацію" in texts_to(calls, CLIENT)[0]

        # Не зображення — бот просить інший файл, а кнопки старої картинки просять надіслати логотип знову.
        assert "Не вдалося розпізнати" in texts_to(await chat.send_file(CLIENT, "BAD", document=True), CLIENT)[0]
        calls = await chat.press(CLIENT, "lgs:7", on_photo=True)
        assert any(name == "answerCallbackQuery" and "ще раз" in p.get("text", "") for name, p in calls)

    run(scenario)


def test_anna_answers_and_falls_back(run, monkeypatch):
    asked = []

    async def fake_reply(history, question):
        asked.append((list(history), question))
        return None if question == "доставка?" else f"Анна: {question}"

    async def scenario(chat, app):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        monkeypatch.setattr(bot.assistant, "reply", fake_reply)
        calls = await chat.say(CLIENT, "Добрий день")
        assert texts_to(calls, CLIENT) == ["Анна: Добрий день"]
        # Питання про товар — відповідь Анни і кнопка, що відкриває картку товару.
        calls = await chat.say(CLIENT, "Худі оверсайз є?")
        assert texts_to(calls, CLIENT) == ["Анна: Худі оверсайз є?"] and "m:3" in str(calls)
        # Анна пам'ятає розмову.
        assert asked[1][0] == [{"role": "user", "content": "Добрий день"},
                               {"role": "assistant", "content": "Анна: Добрий день"}]
        # Claude недоступний — бот відповідає сам, за ключовими словами.
        assert "Доставка та оплата" in texts_to(await chat.say(CLIENT, "доставка?"), CLIENT)[0]

    run(scenario)


def test_call_manager_button(run, monkeypatch):
    async def scenario(chat, app):
        monkeypatch.setenv("MANAGER_ID", str(MANAGER))
        await chat.say(CLIENT, "Чи можна пошити 30 фартухів зі знижкою?")
        calls = await chat.say(CLIENT, bot.BTN_CALL_MANAGER)
        to_manager = texts_to(calls, MANAGER)[0]
        assert "просить зв'язатися" in to_manager and "@user100" in to_manager
        assert "30 фартухів зі знижкою" in to_manager
        assert "передала ваш запит" in texts_to(calls, CLIENT)[0]
        # Повторне натискання не засипає менеджера однаковими запитами.
        calls = await chat.say(CLIENT, bot.BTN_CALL_MANAGER)
        assert not texts_to(calls, MANAGER) and "уже отримав" in texts_to(calls, CLIENT)[0]

    run(scenario)


def test_cancel_keeps_cart(run):
    async def scenario(chat, app):
        await chat.press(CLIENT, "add:2")
        await chat.press(CLIENT, "checkout")
        assert "скасовано" in texts_to(await chat.say(CLIENT, bot.BTN_CANCEL), CLIENT)[0]
        assert "Футболка базова (чорна)" in texts_to(await chat.say(CLIENT, "/cart"), CLIENT)[0]
        assert app.bot_data["db"].recent_orders() == []

    run(scenario)


def test_repeat_order_reuses_contacts(run):
    async def scenario(chat, app):
        app.bot_data["db"].create_order(CLIENT, "user100", "Ольга", "0501234567",
                                        "🏤 Нова Пошта — відділення, Київ, 5", [("Кепка", 390, 1)])
        await chat.press(CLIENT, "add:2")
        offer = texts_to(await chat.press(CLIENT, "checkout"), CLIENT)[0]
        assert "ті самі дані" in offer and "Ольга" in offer and "Київ, 5" in offer

        # Одна кнопка замість імені, телефону й адреси; коментар можна пропустити.
        assert "оплатити" in texts_to(await chat.say(CLIENT, bot.BTN_SAME_DATA), CLIENT)[0]
        await chat.say(CLIENT, "💵 Накладений платіж")
        summary = texts_to(await chat.say(CLIENT, bot.BTN_SKIP), CLIENT)[0]
        assert "Ольга" in summary and "Накладений платіж" in summary and "Коментар" not in summary
        done = texts_to(await chat.say(CLIENT, bot.BTN_CONFIRM), CLIENT)[0]
        assert "№2 прийнято" in done and "Реквізити" not in done
        assert app.bot_data["db"].get_order(2)["delivery"] == "🏤 Нова Пошта — відділення, Київ, 5"

        # «Ввести нові» веде до звичайних кроків.
        await chat.press(CLIENT, "add:2")
        await chat.press(CLIENT, "checkout")
        assert "звертатися" in texts_to(await chat.say(CLIENT, bot.BTN_NEW_DATA), CLIENT)[0]

    run(scenario)


def test_checkout_with_empty_cart_is_refused(run):
    async def scenario(chat, app):
        calls = await chat.press(CLIENT, "checkout")
        assert [name for name, _ in calls] == ["answerCallbackQuery"]
        # Розмова не почалася: звичайний текст обробляється як питання, а не як ім'я.
        assert "Доставка та оплата" in texts_to(await chat.say(CLIENT, "яка доставка?"), CLIENT)[0]

    run(scenario)


def test_search_and_hits(run, monkeypatch):
    monkeypatch.setattr(bot.texts, "HITS", ["3", "1"])

    async def scenario(chat, app):
        calls = await chat.say(CLIENT, "Хочу білу футболку")
        assert "знайшлося" in texts_to(calls, CLIENT)[0]
        assert "m:1" in str(calls) and "m:2" not in str(calls)
        # Питання не про товар працюють як раніше.
        assert "Доставка та оплата" in texts_to(await chat.say(CLIENT, "яка доставка?"), CLIENT)[0]

        calls = await chat.say(CLIENT, bot.BTN_CATALOG)
        assert "Хіти продажу" in str(calls) and "'hits'" in str(calls)
        calls = await chat.press(CLIENT, "hits")
        assert "Хіти продажу" in texts_to(calls, CLIENT)[0]
        markup = str(calls)
        assert markup.index("m:3") < markup.index("m:1") and "Футболка базова, біла — 450 грн" in markup

    run(scenario)


def test_cart_reminder(run):
    async def scenario(chat, app):
        def reminder_jobs():
            return app.job_queue.get_jobs_by_name(f"cart:{CLIENT}")

        await chat.press(CLIENT, "add:2")
        await chat.press(CLIENT, "add:1")
        # Кожне додавання переносить нагадування, а не створює ще одне.
        jobs = [j for j in reminder_jobs() if not j.removed]
        assert len(jobs) == 1

        chat.request.calls.clear()
        await jobs[0].run(app)
        assert "залишили товари" in texts_to(chat.request.calls, CLIENT)[0]

        # Порожній кошик — нагадування не надсилається.
        await chat.press(CLIENT, "clear")
        chat.request.calls.clear()
        await jobs[0].run(app)
        assert texts_to(chat.request.calls, CLIENT) == []

        # Після оформлення нагадування скасовується.
        await chat.press(CLIENT, "add:2")
        await chat.press(CLIENT, "checkout")
        for answer in ("Ольга", "0501234567", "🏤 Нова Пошта — відділення", "Київ, 5", "💵 Накладений платіж",
                       bot.BTN_SKIP, bot.BTN_CONFIRM):
            await chat.say(CLIENT, answer)
        assert all(j.removed for j in reminder_jobs())

    run(scenario)


def test_rating_after_done(run, monkeypatch):
    monkeypatch.setenv("MANAGER_ID", str(MANAGER))

    async def scenario(chat, app):
        db = app.bot_data["db"]
        db.create_order(CLIENT, "user100", "Ольга", "0501234567", "Київ, 5", [("Кепка", 390, 1)])

        calls = await chat.press(MANAGER, "st:1:done")
        assert any("Оцініть" in t for t in texts_to(calls, CLIENT)) and "rate:1:5" in str(calls)

        await chat.press(STRANGER, "rate:1:1")
        assert db.get_order(1)["rating"] is None

        calls = await chat.press(CLIENT, "rate:1:4")
        assert "Дякуємо за оцінку ⭐⭐⭐⭐" in texts_to(calls, CLIENT)[0]
        assert "Відгук на замовлення №1: ⭐⭐⭐⭐" in texts_to(calls, MANAGER)[0]
        assert "вже оцінили" in str(await chat.press(CLIENT, "rate:1:1"))
        assert db.get_order(1)["rating"] == 4

        assert "Середня оцінка: 4.0" in texts_to(await chat.say(MANAGER, "/stats"), MANAGER)[0]
        assert "Оцінка клієнта: ⭐⭐⭐⭐" in texts_to(await chat.press(MANAGER, "ord:1"), MANAGER)[0]

    run(scenario)


def test_report_export_and_daily_job(run, monkeypatch):
    monkeypatch.setenv("MANAGER_ID", str(MANAGER))

    async def scenario(chat, app):
        assert "лише менеджеру" in texts_to(await chat.say(CLIENT, "/export"), CLIENT)[0]
        assert "Замовлень ще немає" in texts_to(await chat.say(MANAGER, "/export"), MANAGER)[0]

        db = app.bot_data["db"]
        db.create_order(CLIENT, "user100", "Ольга", "0501234567", "Київ, 5", [("Кепка", 390, 2)])
        assert "Замовлень: 1" in texts_to(await chat.say(MANAGER, "/report"), MANAGER)[0]

        calls = await chat.say(MANAGER, "/export")
        assert [p["caption"] for name, p in calls if name == "sendDocument"] == ["📄 Усі замовлення (1 шт.)"]

        # Щоденний звіт заплановано на 16:00 за Києвом.
        [job] = app.job_queue.get_jobs_by_name("daily_report")
        assert job.trigger.fields[5].expressions[0].first == 16  # hour
        chat.request.calls.clear()
        await job.run(app)
        assert "Звіт за добу" in texts_to(chat.request.calls, MANAGER)[0]

    run(scenario)


def test_errors_are_reported_to_manager(run, monkeypatch):
    monkeypatch.setenv("MANAGER_ID", str(MANAGER))

    def broken():
        raise RuntimeError("каталог зламався")

    async def scenario(chat, app):
        monkeypatch.setattr(bot.shop, "load_products", broken)
        calls = await chat.say(CLIENT, bot.BTN_CATALOG)
        await asyncio.sleep(0.1)  # обробник помилок працює окремим завданням
        calls += chat.request.calls
        assert "щось пішло не так" in texts_to(calls, CLIENT)[0]
        to_manager = texts_to(calls, MANAGER)[0]
        assert "Помилка в боті" in to_manager and "каталог зламався" in to_manager

    run(scenario)
