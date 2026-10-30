"""Безкоштовний Telegram-бот магазину: каталог, кошик, замовлення, панель менеджера.
Запуск: python bot.py"""
import asyncio
import hashlib
import html
import logging
import os
import re
import traceback
import warnings
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, ChatMigrated, Forbidden
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          ConversationHandler, MessageHandler, filters)
from telegram.warnings import PTBUserWarning

import assistant
import logo
import reports
import shop
import texts
from db import KYIV, Storage

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

BTN_CATALOG = "🛍 Каталог"
BTN_CART = "🧺 Кошик"
BTN_DELIVERY = "🚚 Доставка"
BTN_CONTACTS = "📞 Контакти"
BTN_MY_ORDERS = "📦 Мої замовлення"
BTN_CANCEL = "❌ Скасувати"
BTN_CONFIRM = "✅ Підтвердити"
BTN_ASK_MANAGER = "📞 Замовити через менеджера"
BTN_SIZE_CHART = "📏 Розмірна сітка"
BTN_WRITE_MANAGER = "💬 Написати менеджеру"
BTN_SAME_DATA = "✅ Так, ті самі дані"
BTN_NEW_DATA = "✏️ Ввести нові"
BTN_SKIP = "⏭ Пропустити"
BTN_LOGO = "🎨 Логотип на одязі"
BTN_TRY_LOGO = "🎨 Приміряти логотип"
BTN_NEW_LOGO = "🔄 Інший логотип"
BTN_OTHER_ITEM = "👕 Інший виріб"
BTN_LOGO_ORDER = "✅ Замовити з логотипом"
BTN_CALL_MANAGER = "💬 Зв'язатися з менеджером"
RECENT_MESSAGES = 5  # скільки останніх повідомлень клієнта бачить менеджер у запиті на зв'язок

# Під полем введення — лише зв'язок з менеджером; решту клієнт відкриває, написавши Анні, або командами.
MENU = ReplyKeyboardMarkup([[BTN_CALL_MANAGER]],
                           resize_keyboard=True)
CANCEL_KB = ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)
PHONE_KB = ReplyKeyboardMarkup([[KeyboardButton("📱 Надіслати мій номер", request_contact=True)], [BTN_CANCEL]],
                               resize_keyboard=True)
CONFIRM_KB = ReplyKeyboardMarkup([[BTN_CONFIRM, BTN_CANCEL]], resize_keyboard=True)
REUSE_KB = ReplyKeyboardMarkup([[BTN_SAME_DATA], [BTN_NEW_DATA], [BTN_CANCEL]], resize_keyboard=True)
DELIVERY_KB = ReplyKeyboardMarkup([[m] for m in texts.DELIVERY_METHODS] + [[BTN_CANCEL]], resize_keyboard=True)
PAYMENT_KB = ReplyKeyboardMarkup([[m] for m in texts.PAYMENT_METHODS] + [[BTN_CANCEL]], resize_keyboard=True)
COMMENT_KB = ReplyKeyboardMarkup([[BTN_SKIP], [BTN_CANCEL]], resize_keyboard=True)

REUSE, NAME, PHONE, DELIVERY_METHOD, DELIVERY, PAYMENT, COMMENT, CONFIRM = range(8)


def storage(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.bot_data["db"]


def get_cart(context: ContextTypes.DEFAULT_TYPE) -> dict[str, int]:
    return context.user_data.setdefault("cart", {})


def manager_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    # MANAGER_ID зі змінних середовища має пріоритет: на хостингу база може очищатися.
    value = os.getenv("MANAGER_ID", "").strip() or storage(context).get_setting("manager_id")
    return int(value) if value else None


def orders_chat_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    # Окрема група для нових замовлень; без неї замовлення йдуть менеджеру в особистий чат.
    # Збережене в базі значення новіше за ORDERS_CHAT_ID: його ставить /orderschat або зміна номера групи.
    value = storage(context).get_setting("orders_chat_id") or os.getenv("ORDERS_CHAT_ID", "").strip()
    return int(value) if value else None


def is_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat.id
    return chat == manager_id(context) or chat == orders_chat_id(context)


async def edit_or_ignore(coro) -> None:
    """Telegram відхиляє редагування, яке нічого не змінює, — це не помилка."""
    try:
        await coro
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ---------- Вигляд повідомлень ----------

def categories_view(products: dict[str, shop.Product]) -> tuple[str, InlineKeyboardMarkup]:
    buttons = [[InlineKeyboardButton(name, callback_data=f"c:{i}")]
               for i, name in enumerate(shop.categories(products))]
    if shop.hits(products):
        buttons.insert(0, [InlineKeyboardButton(texts.HITS_BUTTON, callback_data="hits")])
    return texts.CATALOG_TITLE, InlineKeyboardMarkup(buttons)


def product_buttons(found: list[shop.Product]) -> list[list[InlineKeyboardButton]]:
    """Окремі товари (модель + колір) — для хітів і результатів пошуку."""
    return [[InlineKeyboardButton(f"{p.model}{', ' + p.color.lower() if p.color else ''} — {p.price} грн",
                                  callback_data=f"m:{p.id}")] for p in found]


def hits_view(products: dict[str, shop.Product]) -> tuple[str, InlineKeyboardMarkup]:
    buttons = product_buttons(shop.hits(products))
    buttons.append([InlineKeyboardButton(texts.BTN_ALL_CATEGORIES, callback_data="cats")])
    return texts.HITS_TITLE, InlineKeyboardMarkup(buttons)


def search_view(found: list[shop.Product]) -> tuple[str, InlineKeyboardMarkup]:
    text = texts.SEARCH_FOUND
    if len(found) > texts.SEARCH_LIMIT:
        text += "\n" + texts.SEARCH_MORE.format(shown=texts.SEARCH_LIMIT, total=len(found))
    return text, InlineKeyboardMarkup(product_buttons(found[:texts.SEARCH_LIMIT]))


def models_view(products: dict[str, shop.Product], category: str) -> tuple[str, InlineKeyboardMarkup]:
    buttons = []
    for model, variants in shop.models(products, category).items():
        prices = {v.price for v in variants}
        price = f"{min(prices)} грн" if len(prices) == 1 else f"від {min(prices)} грн"
        buttons.append([InlineKeyboardButton(f"{model} — {price}", callback_data=f"m:{variants[0].id}")])
    buttons.append([InlineKeyboardButton(texts.BTN_ALL_CATEGORIES, callback_data="cats")])
    return texts.MODELS_TITLE.format(category=category), InlineKeyboardMarkup(buttons)


def product_caption(p: shop.Product, size: str = "") -> str:
    lines = [f"{p.model} — {p.price} грн"]
    if p.color:
        lines.append(f"Колір: {p.color}")
    if size:
        lines.append(f"Розмір: {size}")
    elif p.sizes:
        lines.append(texts.CHOOSE_SIZE)
    if p.needs_manager(size):
        lines.append(texts.ON_REQUEST)
    elif not p.in_stock:
        lines.append(texts.OUT_OF_STOCK)
    return "\n".join(lines) + f"\n\n{p.description}"


def product_markup(p: shop.Product, products: dict[str, shop.Product], cart: dict[str, int],
                   size: str = "") -> InlineKeyboardMarkup:
    """Кольори → розміри → розмірна сітка → «Додати» або «Замовити через менеджера» і кошик.
    Вибраний розмір передається в кнопках, тож бот нічого не мусить запам'ятовувати."""
    rows = []
    variants = shop.variants(products, p)
    if len(variants) > 1:
        colors = [InlineKeyboardButton(("✓ " if v.id == p.id else "") + v.color + (" 🕓" if v.on_request else ""),
                                       callback_data=f"v:{v.id}:{size}") for v in variants]
        rows += [colors[i:i + 3] for i in range(0, len(colors), 3)]
    if p.sizes:
        sizes = [InlineKeyboardButton(("✓ " if s == size else "") + s + (" 🕓" if s in p.request_sizes else ""),
                                      callback_data=f"s:{p.id}:{s}") for s in p.sizes]
        rows += [sizes[i:i + 4] for i in range(0, len(sizes), 4)]
    if shop.chart_path(p):
        rows.append([InlineKeyboardButton(BTN_SIZE_CHART, callback_data=f"chart:{p.id}")])
    if p.id in logo.load_placements():
        rows.append([InlineKeyboardButton(BTN_TRY_LOGO, callback_data=f"lg:{p.id}")])
    row = []
    if p.needs_manager(size):
        row.append(InlineKeyboardButton(BTN_ASK_MANAGER, callback_data=f"ask:{p.id}:{size}"))
    elif p.in_stock:
        qty = cart.get(shop.cart_key(p.id, size), 0)
        row.append(InlineKeyboardButton(f"➕ Додати (у кошику: {qty})" if qty else "➕ Додати",
                                        callback_data=f"add:{shop.cart_key(p.id, size)}"))
    row.append(InlineKeyboardButton(BTN_CART, callback_data="show"))
    rows.append(row)
    return InlineKeyboardMarkup(rows)


def photo_input(context: ContextTypes.DEFAULT_TYPE, path: Path) -> tuple[tuple, str | Path]:
    """Файл завантажується в Telegram один раз, далі надсилаємо його file_id — так швидше.
    У ключі є час зміни файлу, тож нове фото з тією ж назвою підхопиться саме."""
    key = (str(path), path.stat().st_mtime_ns)
    return key, context.bot_data.setdefault("photo_ids", {}).get(key, path)


def remember_photo(context: ContextTypes.DEFAULT_TYPE, key: tuple, message) -> None:
    if message and message.photo:
        context.bot_data["photo_ids"][key] = message.photo[-1].file_id


async def send_product(message, context: ContextTypes.DEFAULT_TYPE, p: shop.Product,
                       products: dict[str, shop.Product]) -> None:
    caption, markup = product_caption(p), product_markup(p, products, get_cart(context))
    path = shop.photo_path(p.id)
    if not path:
        await message.reply_text(caption, reply_markup=markup)
        return
    key, photo = photo_input(context, path)
    remember_photo(context, key, await message.reply_photo(photo, caption=caption, reply_markup=markup))


async def switch_variant(message, context: ContextTypes.DEFAULT_TYPE, p: shop.Product,
                         products: dict[str, shop.Product], size: str = "", new_photo: bool = True) -> None:
    """Оновлює ту саму картку: інший колір міняє фото, інший розмір — лише підпис і кнопки."""
    caption, markup = product_caption(p, size), product_markup(p, products, get_cart(context), size)
    path = shop.photo_path(p.id)
    if path and message.photo and new_photo:
        key, photo = photo_input(context, path)
        # У InputMediaPhoto шлях до файлу перетворюється на посилання file://, яке Telegram не приймає,
        # тому передаємо сам вміст файлу — так фото справді завантажується.
        if isinstance(photo, Path):
            photo = photo.read_bytes()
        sent = await message.edit_media(InputMediaPhoto(photo, caption=caption), reply_markup=markup)
        remember_photo(context, key, sent)
    elif message.photo:
        await edit_or_ignore(message.edit_caption(caption, reply_markup=markup))
    elif path:
        # Текстову картку не можна перетворити на фото — надсилаємо нову.
        await send_product(message, context, p, products)
    else:
        await edit_or_ignore(message.edit_text(caption, reply_markup=markup))


def items_text(items: list[tuple[str, int, int]]) -> str:
    """items — список (назва, ціна, кількість)."""
    return "\n".join(f"• {title} — {qty} × {price} = {qty * price} грн" for title, price, qty in items)


def cart_view(cart: dict[str, int], products: dict[str, shop.Product]) -> tuple[str, InlineKeyboardMarkup | None]:
    lines = shop.cart_lines(cart, products)
    if not lines:
        return texts.CART_EMPTY, None
    total = shop.cart_total(lines)
    text = (f"{texts.CART_TITLE}\n\n{items_text([(p.title, p.price, qty) for p, qty in lines])}\n\n"
            f"Разом: {total} грн\n{shop.delivery_note(total)}")
    buttons = [[InlineKeyboardButton("➖", callback_data=f"dec:{p.id}"),
                InlineKeyboardButton(f"{p.title} × {qty}", callback_data="noop"),
                InlineKeyboardButton("➕", callback_data=f"inc:{p.id}")] for p, qty in lines]
    buttons.append([InlineKeyboardButton("🗑 Очистити", callback_data="clear"),
                    InlineKeyboardButton("✅ Оформити", callback_data="checkout")])
    return text, InlineKeyboardMarkup(buttons)


def contacts_text(order: dict) -> str:
    """Дані клієнта із замовлення; оплата й коментар — лише якщо вказані."""
    lines = [f"Ім'я: {order['name']}", f"Телефон: {order['phone']}", f"Доставка: {order['delivery']}"]
    if order.get("payment"):
        lines.append(f"Оплата: {order['payment']}")
    if order.get("comment"):
        lines.append(f"Коментар: {order['comment']}")
    return "\n".join(lines)


def order_card(order: dict) -> str:
    username = f"@{order['username']}" if order["username"] else "немає"
    items = items_text([(i["title"], i["price"], i["qty"]) for i in order["items"]])
    rating = f"\nОцінка клієнта: {reports.stars(order['rating'])}" if order.get("rating") else ""
    return (f"Замовлення №{order['id']} від {order['created']}\n"
            f"Статус: {texts.STATUSES[order['status']]}\n\n"
            f"{items}\nРазом: {order['total']} грн\n\n"
            f"{contacts_text(order)}\n"
            f"Telegram: {username}{rating}")


def rating_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{n} ⭐", callback_data=f"rate:{order_id}:{n}")
                                  for n in range(1, 6)]])


def status_keyboard(order_id: int) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(label, callback_data=f"st:{order_id}:{status}")
               for status, label in texts.STATUSES.items() if status != "new"]
    return InlineKeyboardMarkup([buttons[:2], buttons[2:]])


# ---------- Звичайні повідомлення ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(texts.WELCOME, reply_markup=MENU)


async def catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = categories_view(shop.load_products())
    await update.message.reply_text(text, reply_markup=markup)


async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = cart_view(get_cart(context), shop.load_products())
    await update.message.reply_text(text, reply_markup=markup)


async def delivery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(texts.DELIVERY, reply_markup=MENU)


def manager_chat_markup() -> InlineKeyboardMarkup | ReplyKeyboardMarkup:
    """Кнопка, що відкриває чат з менеджером; якщо посилання не задано — звичайне меню."""
    if not texts.MANAGER_LINK:
        return MENU
    return InlineKeyboardMarkup([[InlineKeyboardButton(BTN_WRITE_MANAGER, url=texts.MANAGER_LINK)]])


async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(texts.CONTACTS, reply_markup=manager_chat_markup())


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orders = storage(context).user_orders(update.effective_user.id)
    if not orders:
        await update.message.reply_text(texts.NO_ORDERS, reply_markup=MENU)
        return
    blocks = [texts.MY_ORDERS_TITLE]
    for o in orders:
        items = ", ".join(f"{i['title']} × {i['qty']}" for i in o["items"])
        blocks.append(f"№{o['id']} від {o['created']} — {o['total']} грн\n"
                      f"{texts.STATUSES[o['status']]}\n{items}")
    await update.message.reply_text("\n\n".join(blocks), reply_markup=MENU)


async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = update.message.text
    recent = context.user_data.setdefault("recent", [])
    recent.append(question[:300])
    del recent[:-RECENT_MESSAGES]

    if assistant.enabled():
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        history = context.user_data.setdefault("anna", [])
        answer = await assistant.reply(history, question)
        if answer:
            assistant.remember(history, question, answer)
            # Питання про конкретний товар — під відповіддю кнопки, що відкривають його картку.
            found = shop.search(shop.load_products(), question)
            markup = InlineKeyboardMarkup(product_buttons(found[:texts.SEARCH_LIMIT])) if found else MENU
            await update.message.reply_text(answer, reply_markup=markup)
            return
        # Claude недоступний — відповідаємо за ключовими словами, як без Анни.

    topic = shop.find_topic(question)
    # «Latte бежевий», «купити кітель Rio» — шукаємо товар; питання про доставку чи контакти — ні.
    if topic in (None, "prices", "order"):
        found = shop.search(shop.load_products(), update.message.text)
        if found:
            text, markup = search_view(found)
            await update.message.reply_text(text, reply_markup=markup)
            return
    if topic == "prices":
        await catalog(update, context)
        return
    if topic == "cart":
        await show_cart(update, context)
        return
    if topic == "logo":
        _, markup = logo_models_view(shop.load_products())
        await update.message.reply_text(texts.LOGO_ANSWER, reply_markup=markup)
        return
    answers = {
        "delivery": texts.DELIVERY,
        "contacts": texts.CONTACTS,
        "order": texts.ORDER_HINT,
        "manager": texts.MANAGER_HINT,
        "logo": texts.LOGO_ANSWER,
        "sizes": texts.SIZES_ANSWER,
        "who": texts.WHO_ANSWER,
        "thanks": texts.THANKS,
        "greeting": texts.WELCOME,
    }
    await update.message.reply_text(answers.get(topic, texts.UNKNOWN), reply_markup=MENU)


async def call_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Менеджер отримує запит на зв'язок разом з останніми повідомленнями клієнта, щоб не перепитувати."""
    now = datetime.now().timestamp()
    if now - context.user_data.get("manager_called", 0) < texts.MANAGER_CALL_COOLDOWN_MIN * 60:
        await update.message.reply_text(texts.MANAGER_CALL_REPEAT, reply_markup=manager_chat_markup())
        return
    user = update.effective_user
    username = f"@{user.username}" if user.username else "немає"
    text = (f"{texts.MANAGER_CALL_REQUEST}\n\n"
            f"Клієнт: <a href=\"tg://user?id={user.id}\">{html.escape(user.full_name)}</a>\n"
            f"Telegram: {username}")
    recent = context.user_data.get("recent", [])
    if recent:
        text += f"\n\n{texts.MANAGER_CALL_HISTORY}\n" + "\n".join(f"• {html.escape(m)}" for m in recent)

    async def send(chat_id: int, note: str) -> None:
        await context.bot.send_message(chat_id, f"{note}\n\n{text}" if note else text, parse_mode=ParseMode.HTML)

    sent = await notify_orders(context, send)
    if sent:
        context.user_data["manager_called"] = now
    await update.message.reply_text(texts.MANAGER_CALL_SENT if sent else texts.ASK_CONTACTS,
                                    reply_markup=manager_chat_markup())


# ---------- Каталог (inline-кнопки) ----------

async def catalog_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cats — розділи, hits — хіти продажу, c:<№> — моделі розділу, m:<id> — картка моделі,
    v:<id>:<розмір> — інший колір, s:<id>:<розмір> — інший розмір, chart:<id> — розмірна сітка."""
    query = update.callback_query
    action, _, arg = query.data.partition(":")
    pid, _, size = arg.partition(":")
    products = shop.load_products()
    categories = shop.categories(products)
    p = products.get(pid)
    if action == "cats":
        await query.answer()
        text, markup = categories_view(products)
        await edit_or_ignore(query.edit_message_text(text, reply_markup=markup))
    elif action == "hits":
        await query.answer()
        text, markup = hits_view(products)
        await edit_or_ignore(query.edit_message_text(text, reply_markup=markup))
    elif action == "c" and int(arg) < len(categories):
        await query.answer()
        text, markup = models_view(products, categories[int(arg)])
        await edit_or_ignore(query.edit_message_text(text, reply_markup=markup))
    elif action == "m" and p:
        await query.answer()
        await send_product(query.message, context, p, products)
    elif action == "v" and p:
        await query.answer(p.color)
        await switch_variant(query.message, context, p, products, size if size in p.sizes else "")
    elif action == "s" and p and size in p.sizes:
        await query.answer(f"Розмір {size}")
        await switch_variant(query.message, context, p, products, size, new_photo=False)
    elif action == "chart" and p and shop.chart_path(p):
        await query.answer()
        key, photo = photo_input(context, shop.chart_path(p))
        remember_photo(context, key, await query.message.reply_photo(
            photo, caption=texts.SIZE_CHART.format(model=p.model)))
    else:
        await query.answer(texts.CATALOG_CHANGED, show_alert=True)


async def ask_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Товар під замовлення: надсилаємо менеджеру запит, а клієнту — контакти."""
    query = update.callback_query
    pid, _, size = query.data.removeprefix("ask:").partition(":")
    products = shop.load_products()
    if pid not in products:
        await query.answer(texts.CATALOG_CHANGED, show_alert=True)
        return
    await query.answer()
    p = replace(products[pid], size=size)
    user = update.effective_user
    manager = manager_id(context)
    if manager:
        username = f"@{user.username}" if user.username else "немає"
        try:
            await context.bot.send_message(
                manager,
                f"{texts.NEW_REQUEST}\n\n{html.escape(p.title)} — {p.price} грн\n\n"
                f"Клієнт: <a href=\"tg://user?id={user.id}\">{html.escape(user.full_name)}</a>\n"
                f"Telegram: {username}",
                parse_mode=ParseMode.HTML)
        except Exception:
            logging.exception("Не вдалося надіслати менеджеру запит на товар під замовлення")
            manager = None
    await query.message.reply_text(texts.ASK_SENT if manager else texts.ASK_CONTACTS, reply_markup=manager_chat_markup())


# ---------- Логотип на одязі ----------
# Стан клієнта — у user_data["logo"]: pid (виріб), size (см), spot (місце), file_id і kind (photo/document)
# надісланого логотипа, art — вже підготовлений логотип, щоб не обробляти його при кожному натисканні.

def logo_models_view(products: dict[str, shop.Product]) -> tuple[str, InlineKeyboardMarkup]:
    models = logo.logo_products(products, logo.load_placements())
    buttons = [[InlineKeyboardButton(model, callback_data=f"lg:{variants[0].id}")] for model, variants in models.items()]
    return texts.LOGO_CHOOSE_MODEL, InlineKeyboardMarkup(buttons)


def logo_markup(p: shop.Product, products: dict[str, shop.Product], placements: dict[str, logo.Placement],
                state: dict) -> InlineKeyboardMarkup:
    """Кольори виробу → розміри логотипа → місця → інший логотип / виріб → замовити."""
    rows = []
    variants = logo.logo_products(products, placements).get(p.model, [])
    if len(variants) > 1:
        colors = [InlineKeyboardButton(("✓ " if v.id == p.id else "") + v.color, callback_data=f"lgc:{v.id}")
                  for v in variants]
        rows += [colors[i:i + 3] for i in range(0, len(colors), 3)]
    largest = texts.LOGO_SPOTS[state["spot"]][1]
    rows.append([InlineKeyboardButton(("✓ " if cm == state["size"] else "") + f"{cm} см", callback_data=f"lgs:{cm}")
                 for cm in texts.LOGO_SIZES if cm <= largest])
    spots = [InlineKeyboardButton(("✓ " if s == state["spot"] else "") + texts.LOGO_SPOTS[s][0], callback_data=f"lgw:{s}")
             for s in placements[p.id].spots]
    if len(spots) > 1:
        rows.append(spots)
    rows.append([InlineKeyboardButton(BTN_NEW_LOGO, callback_data="lgn"),
                 InlineKeyboardButton(BTN_OTHER_ITEM, callback_data="lgm")])
    rows.append([InlineKeyboardButton(BTN_LOGO_ORDER, callback_data="lgo")])
    return InlineKeyboardMarkup(rows)


def choose_logo_item(state: dict, pid: str, placements: dict[str, logo.Placement]) -> None:
    """Новий виріб: місце лишається, якщо воно в нього є, а розмір — якщо вміщається на це місце."""
    spots = list(placements[pid].spots)
    if state.get("spot") not in spots:
        state["spot"] = spots[0]
    state["size"] = min(state.get("size", texts.LOGO_DEFAULT_SIZE), texts.LOGO_SPOTS[state["spot"]][1])
    state["pid"] = pid


async def logo_preview(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       state: dict) -> tuple[bytes, str, InlineKeyboardMarkup] | None:
    """Готує візуалізацію; якщо логотип не вдалося розпізнати — повідомляє клієнта й повертає None."""
    products, placements = shop.load_products(), logo.load_placements()
    p = products[state["pid"]]
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
    if "art" not in state:
        data = await (await context.bot.get_file(state["file_id"])).download_as_bytearray()
        try:
            state["art"] = await asyncio.to_thread(logo.prepare_logo, bytes(data))
        except ValueError:
            state.pop("file_id", None)
            await update.effective_message.reply_text(texts.LOGO_BAD_FILE)
            return None
    jpg, recolored = await asyncio.to_thread(logo.render, shop.photo_path(p.id), state["art"],
                                             placements[p.id], state["spot"], state["size"])
    caption = texts.LOGO_CAPTION.format(title=p.title, price=p.price, size=state["size"],
                                        spot=texts.LOGO_SPOTS[state["spot"]][0].lower())
    if recolored:
        caption += "\n\n" + texts.LOGO_RECOLORED
    return jpg, f"{caption}\n\n{texts.LOGO_HINT}", logo_markup(p, products, placements, state)


async def logo_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = logo_models_view(shop.load_products())
    await update.message.reply_text(text, reply_markup=markup)


async def logo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    state = context.user_data.setdefault("logo", {})
    if message.photo:
        state.update(file_id=message.photo[-1].file_id, kind="photo")
    else:
        state.update(file_id=message.document.file_id, kind="document")
    state.pop("art", None)
    if state.get("pid") not in logo.load_placements():
        text, markup = logo_models_view(shop.load_products())
        await message.reply_text(text, reply_markup=markup)
        return
    preview = await logo_preview(update, context, state)
    if preview:
        jpg, caption, markup = preview
        await message.reply_photo(jpg, caption=caption, reply_markup=markup)


async def logo_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """lg:<id> — обрати виріб, lgc:<id> — інший колір, lgs:<см> — розмір, lgw:<місце> — місце,
    lgn — інший логотип, lgm — інший виріб, lgo — надіслати менеджеру."""
    query = update.callback_query
    action, _, arg = query.data.partition(":")
    state = context.user_data.setdefault("logo", {})
    products, placements = shop.load_products(), logo.load_placements()

    if action == "lgm":
        await query.answer()
        text, markup = logo_models_view(products)
        await query.message.reply_text(text, reply_markup=markup)
        return
    if action == "lgn":
        await query.answer()
        state.pop("file_id", None)
        state.pop("art", None)
        await query.message.reply_text(texts.LOGO_SEND)
        return
    if action in ("lg", "lgc"):
        if arg not in placements or arg not in products:
            await query.answer(texts.CATALOG_CHANGED, show_alert=True)
            return
        choose_logo_item(state, arg, placements)
        if "file_id" not in state:
            await query.answer()
            await query.message.reply_text(texts.LOGO_SEND)
            return
    elif "file_id" not in state or state.get("pid") not in placements:
        # Після перезапуску бота стан губиться — просимо надіслати логотип ще раз.
        await query.answer(texts.LOGO_EXPIRED, show_alert=True)
        return
    elif action == "lgs" and arg.isdigit() and int(arg) in texts.LOGO_SIZES:
        state["size"] = min(int(arg), texts.LOGO_SPOTS[state["spot"]][1])
    elif action == "lgw" and arg in placements[state["pid"]].spots:
        state["spot"] = arg
        state["size"] = min(state["size"], texts.LOGO_SPOTS[arg][1])
    elif action == "lgo":
        await send_logo_request(update, context, state)
        return
    else:
        await query.answer()
        return

    await query.answer()
    preview = await logo_preview(update, context, state)
    if not preview:
        return
    jpg, caption, markup = preview
    # Вибір виробу зі списку чи картки — нове повідомлення; зміна кольору, розміру, місця — оновлення картинки.
    if action == "lg" or not query.message.photo:
        await query.message.reply_photo(jpg, caption=caption, reply_markup=markup)
    else:
        await query.message.edit_media(InputMediaPhoto(jpg, caption=caption), reply_markup=markup)


async def send_logo_request(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    """Менеджер отримує візуалізацію, параметри й оригінал логотипа для виробництва."""
    query = update.callback_query
    await query.answer()
    p = shop.load_products()[state["pid"]]
    user = update.effective_user
    username = f"@{user.username}" if user.username else "немає"
    caption = (f"{texts.LOGO_NEW_REQUEST}\n\n{html.escape(p.title)} — {p.price} грн\n"
               f"Логотип: {state['size']} см, {texts.LOGO_SPOTS[state['spot']][0].lower()}\n\n"
               f"Клієнт: <a href=\"tg://user?id={user.id}\">{html.escape(user.full_name)}</a>\n"
               f"Telegram: {username}")
    preview = query.message.photo[-1].file_id

    async def send(chat_id: int, note: str) -> None:
        await context.bot.send_photo(chat_id, preview, caption=f"{note}\n\n{caption}" if note else caption,
                                     parse_mode=ParseMode.HTML)
        if state["kind"] == "document":
            await context.bot.send_document(chat_id, state["file_id"], caption=texts.LOGO_ORIGINAL)
        else:
            await context.bot.send_photo(chat_id, state["file_id"], caption=texts.LOGO_ORIGINAL)

    sent = await notify_orders(context, send)
    await query.message.reply_text(texts.LOGO_SENT if sent else texts.ASK_CONTACTS, reply_markup=manager_chat_markup())


# ---------- Кошик (inline-кнопки) ----------

def schedule_cart_reminder(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Кожне додавання товару переносить нагадування: воно надійде через CART_REMINDER_HOURS
    після останньої дії. Нагадування живе в пам'яті, тож після перезапуску бота зникає."""
    if not context.job_queue:
        return
    cancel_cart_reminder(context, user_id)
    context.job_queue.run_once(cart_reminder, timedelta(hours=texts.CART_REMINDER_HOURS),
                               chat_id=user_id, user_id=user_id, name=f"cart:{user_id}")


def cancel_cart_reminder(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    if context.job_queue:
        for job in context.job_queue.get_jobs_by_name(f"cart:{user_id}"):
            job.schedule_removal()


async def cart_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not shop.cart_lines(context.user_data.get("cart", {}), shop.load_products()):
        return
    try:
        await context.bot.send_message(
            context.job.chat_id, texts.CART_REMINDER,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(texts.BTN_OPEN_CART, callback_data="show")]]))
    except Forbidden:
        pass  # клієнт заблокував бота — нагадувати нікому



async def cart_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    # key — позиція кошика: id товару, а для одягу з розмірами ще й розмір ("12:M").
    action, _, key = query.data.partition(":")
    pid, size = shop.parse_key(key)
    products = shop.load_products()
    cart = get_cart(context)
    p = products.get(pid)

    if action == "noop":
        await query.answer()
    elif action == "show":
        await query.answer()
        text, markup = cart_view(cart, products)
        await query.message.reply_text(text, reply_markup=markup)
    elif action == "add" and p and p.sizes and not size:
        await query.answer(texts.CHOOSE_SIZE_FIRST, show_alert=True)
    elif action in ("add", "inc") and not (p and p.can_buy(size)):
        await query.answer(texts.NOT_AVAILABLE, show_alert=True)
    elif action in ("add", "inc") and cart.get(key, 0) >= shop.MAX_QTY:
        await query.answer(texts.MAX_QTY_REACHED, show_alert=True)
    elif action == "add":
        cart[key] = cart.get(key, 0) + 1
        schedule_cart_reminder(context, update.effective_user.id)
        await query.answer(f"Додано: {replace(p, size=size).title}")
        await edit_or_ignore(query.edit_message_reply_markup(product_markup(p, products, cart, size)))
    else:
        if action == "inc":
            cart[key] = cart.get(key, 0) + 1
            schedule_cart_reminder(context, update.effective_user.id)
        elif action == "dec" and key in cart:
            cart[key] -= 1
            if cart[key] <= 0:
                del cart[key]
        elif action == "clear":
            cart.clear()
        await query.answer()
        text, markup = cart_view(cart, products)
        await edit_or_ignore(query.edit_message_text(text, reply_markup=markup))


# ---------- Оформлення замовлення ----------

async def checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not shop.cart_lines(get_cart(context), shop.load_products()):
        await query.answer(texts.CART_EMPTY, show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["order"] = {}
    # Клієнт уже замовляв — пропонуємо не вводити ім'я, телефон і адресу знову.
    previous = storage(context).last_contacts(update.effective_user.id)
    if previous:
        context.user_data["previous"] = previous
        await query.message.reply_text(f"{texts.REUSE_CONTACTS}\n\n{contacts_text(previous)}", reply_markup=REUSE_KB)
        return REUSE
    await query.message.reply_text(texts.ASK_NAME, reply_markup=CANCEL_KB)
    return NAME


async def order_reuse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == BTN_SAME_DATA:
        context.user_data["order"].update(context.user_data.pop("previous"))
        await update.message.reply_text(texts.ASK_PAYMENT, reply_markup=PAYMENT_KB)
        return PAYMENT
    if text == BTN_NEW_DATA:
        context.user_data.pop("previous", None)
        await update.message.reply_text(texts.ASK_NAME, reply_markup=CANCEL_KB)
        return NAME
    await update.message.reply_text(texts.USE_BUTTONS, reply_markup=REUSE_KB)
    return REUSE


async def order_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["name"] = update.message.text
    await update.message.reply_text(texts.ASK_PHONE, reply_markup=PHONE_KB)
    return PHONE


async def order_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.contact:
        phone = update.message.contact.phone_number
    else:
        phone = update.message.text
        if len(re.sub(r"\D", "", phone)) < 10:
            await update.message.reply_text(texts.BAD_PHONE, reply_markup=PHONE_KB)
            return PHONE
    context.user_data["order"]["phone"] = phone
    await update.message.reply_text(texts.ASK_DELIVERY_METHOD, reply_markup=DELIVERY_KB)
    return DELIVERY_METHOD


async def order_delivery_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    method = update.message.text
    if method not in texts.DELIVERY_METHODS:
        await update.message.reply_text(texts.USE_BUTTONS, reply_markup=DELIVERY_KB)
        return DELIVERY_METHOD
    context.user_data["order"]["delivery_method"] = method
    await update.message.reply_text(texts.DELIVERY_METHODS[method], reply_markup=CANCEL_KB)
    return DELIVERY


async def order_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order = context.user_data["order"]
    order["delivery"] = f"{order.pop('delivery_method')}, {update.message.text}"
    await update.message.reply_text(texts.ASK_PAYMENT, reply_markup=PAYMENT_KB)
    return PAYMENT


async def order_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    payment = update.message.text
    if payment not in texts.PAYMENT_METHODS:
        await update.message.reply_text(texts.USE_BUTTONS, reply_markup=PAYMENT_KB)
        return PAYMENT
    context.user_data["order"]["payment"] = payment
    await update.message.reply_text(texts.ASK_COMMENT, reply_markup=COMMENT_KB)
    return COMMENT


async def order_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order = context.user_data["order"]
    order["comment"] = "" if update.message.text == BTN_SKIP else update.message.text
    lines = shop.cart_lines(get_cart(context), shop.load_products())
    total = shop.cart_total(lines)
    summary = (f"{texts.CHECK_ORDER}\n\n{items_text([(p.title, p.price, qty) for p, qty in lines])}\n"
               f"Разом: {total} грн\n{shop.delivery_note(total)}\n\n"
               f"{contacts_text(order)}")
    await update.message.reply_text(summary, reply_markup=CONFIRM_KB)
    return CONFIRM


async def notify_orders(context: ContextTypes.DEFAULT_TYPE, send) -> bool:
    """Надсилає в групу замовлень, а якщо її немає чи вона недоступна — менеджеру в особистий чат.
    send(chat_id, note) робить саме надсилання; note — пояснення, чому повідомлення прийшло не в групу.
    Повертає False, якщо надіслати не вдалося нікому."""
    group, manager = orders_chat_id(context), manager_id(context)
    if group:
        try:
            try:
                await send(group, "")
            except ChatMigrated as e:
                # Telegram перетворив групу на супергрупу й видав їй новий номер — запам'ятовуємо його.
                storage(context).set_setting("orders_chat_id", str(e.new_chat_id))
                await send(e.new_chat_id, "")
                if manager:
                    await context.bot.send_message(manager, texts.ORDERS_CHAT_MIGRATED.format(id=e.new_chat_id))
            return True
        except Exception:
            logging.exception("Не вдалося надіслати в групу замовлень %s", group)
    if not manager:
        return False
    try:
        await send(manager, texts.ORDERS_CHAT_FAILED if group else "")
        return True
    except Exception:
        logging.exception("Не вдалося надіслати менеджеру")
        return False


async def send_new_order(context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    text = f"{texts.NEW_ORDER}\n\n{order_card(storage(context).get_order(order_id))}"

    async def send(chat_id: int, note: str) -> None:
        await context.bot.send_message(chat_id, f"{note}\n\n{text}" if note else text,
                                       reply_markup=status_keyboard(order_id))

    if not await notify_orders(context, send):
        logging.warning("Замовлення №%s не надіслано нікому — воно збережене лише в базі", order_id)


async def order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order = context.user_data.pop("order")
    cart = get_cart(context)
    lines = shop.cart_lines(cart, shop.load_products())
    if not lines:
        await update.message.reply_text(texts.CART_EMPTY, reply_markup=MENU)
        return ConversationHandler.END

    user = update.effective_user
    order_id = storage(context).create_order(
        user.id, user.username, order["name"], order["phone"], order["delivery"],
        [(p.title, p.price, qty) for p, qty in lines], order["payment"], order["comment"])
    cart.clear()
    cancel_cart_reminder(context, user.id)

    await send_new_order(context, order_id)

    done = texts.ORDER_DONE.format(id=order_id)
    if order["payment"] == texts.PAYMENT_METHODS[1]:
        done += f"\n{texts.CARD_PAYMENT_NOTE}"
    await update.message.reply_text(done, reply_markup=MENU)
    return ConversationHandler.END


async def order_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("order", None)
    context.user_data.pop("previous", None)
    await update.message.reply_text(texts.ORDER_CANCELLED, reply_markup=MENU)
    return ConversationHandler.END


# ---------- Панель менеджера ----------

async def become_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = manager_id(context)
    if current is None:
        storage(context).set_setting("manager_id", str(update.effective_chat.id))
        await update.message.reply_text(texts.MANAGER_SET)
    elif current == update.effective_chat.id:
        await update.message.reply_text(texts.MANAGER_ALREADY)
    else:
        await update.message.reply_text(texts.MANAGER_TAKEN)


async def set_orders_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Викликається в групі: перевіряємо саме людину, бо чат групи ще не відомий боту.
    if update.effective_user.id != manager_id(context):
        await update.message.reply_text(texts.MANAGER_ONLY)
        return
    if update.effective_chat.type == "private":
        await update.message.reply_text(texts.ORDERS_CHAT_GROUP_ONLY)
        return
    chat_id = update.effective_chat.id
    storage(context).set_setting("orders_chat_id", str(chat_id))
    await update.message.reply_text(texts.ORDERS_CHAT_SET.format(id=chat_id))


async def manager_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_manager(update, context):
        await update.message.reply_text(texts.MANAGER_ONLY)
        return
    orders = storage(context).recent_orders()
    if not orders:
        await update.message.reply_text(texts.NO_ORDERS_YET)
        return
    lines = ["📋 Останні замовлення (натисніть номер, щоб відкрити):\n"]
    lines += [f"№{o['id']} · {o['created']} · {o['name']} · {o['total']} грн · {texts.STATUSES[o['status']]}"
              for o in orders]
    buttons = [InlineKeyboardButton(f"№{o['id']}", callback_data=f"ord:{o['id']}") for o in orders]
    await update.message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup([buttons[:5], buttons[5:]]))


async def manager_open_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_manager(update, context):
        await query.answer(texts.MANAGER_ONLY, show_alert=True)
        return
    order_id = int(query.data.split(":")[1])
    order = storage(context).get_order(order_id)
    if not order:
        await query.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    await query.answer()
    await query.message.reply_text(order_card(order), reply_markup=status_keyboard(order_id))


async def manager_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_manager(update, context):
        await query.answer(texts.MANAGER_ONLY, show_alert=True)
        return
    _, order_id, status = query.data.split(":")
    order = storage(context).get_order(int(order_id))
    if not order or status not in texts.STATUSES:
        await query.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    await query.answer(texts.STATUSES[status])
    if order["status"] == status:
        return
    storage(context).set_status(order["id"], status)
    order["status"] = status
    await edit_or_ignore(query.edit_message_text(order_card(order), reply_markup=status_keyboard(order["id"])))
    try:
        await context.bot.send_message(
            order["user_id"], texts.STATUS_CHANGED.format(id=order["id"], status=texts.STATUSES[status]))
        if status == "done" and not order["rating"]:
            await context.bot.send_message(order["user_id"], texts.ASK_RATING.format(id=order["id"]),
                                           reply_markup=rating_keyboard(order["id"]))
    except Exception:
        logging.exception("Не вдалося сповістити клієнта про статус замовлення №%s", order["id"])


async def rate_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """rate:<№ замовлення>:<1–5> — клієнт оцінює своє виконане замовлення."""
    query = update.callback_query
    _, order_id, rating = query.data.split(":")
    order = storage(context).get_order(int(order_id))
    if not order or order["user_id"] != update.effective_user.id:
        await query.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    if order["rating"]:
        await query.answer(texts.ALREADY_RATED, show_alert=True)
        return
    storage(context).set_rating(order["id"], int(rating))
    stars = reports.stars(int(rating))
    await query.answer()
    await edit_or_ignore(query.edit_message_text(texts.THANKS_FOR_RATING.format(stars=stars)))
    manager = manager_id(context)
    if manager:
        await context.bot.send_message(manager, texts.NEW_RATING.format(id=order["id"], stars=stars))


async def manager_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_manager(update, context):
        await update.message.reply_text(texts.MANAGER_ONLY)
        return
    s = storage(context).stats()
    lines = ["📊 Статистика", "",
             f"Замовлень: {s['orders']}",
             f"Виручка (без скасованих): {s['revenue']} грн",
             f"Середній чек: {s['average']} грн"]
    if s["ratings"]:
        lines.append(f"Середня оцінка: {s['avg_rating']} ⭐ (відгуків: {s['ratings']})")
    if s["by_status"]:
        lines += ["", "За статусами:"]
        lines += [f"{label}: {s['by_status'][status]}" for status, label in texts.STATUSES.items()
                  if status in s["by_status"]]
    if s["top"]:
        lines += ["", "Топ товарів:"]
        lines += [f"{n}. {title} — {qty} шт." for n, (title, qty) in enumerate(s["top"], 1)]
    await update.message.reply_text("\n".join(lines))


def report_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    until = datetime.now(KYIV)
    since = until - timedelta(days=1)
    return reports.daily_report(storage(context).orders_since(since), since, until)


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    manager = manager_id(context)
    if manager:
        await context.bot.send_message(manager, report_text(context))


async def manager_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_manager(update, context):
        await update.message.reply_text(texts.MANAGER_ONLY)
        return
    await update.message.reply_text(report_text(context))


async def manager_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_manager(update, context):
        await update.message.reply_text(texts.MANAGER_ONLY)
        return
    orders = storage(context).all_orders()
    if not orders:
        await update.message.reply_text(texts.NO_ORDERS_YET)
        return
    await update.message.reply_document(
        reports.orders_xlsx(orders), filename=f"orders_{datetime.now(KYIV):%Y-%m-%d}.xlsx",
        caption=texts.EXPORT_CAPTION.format(count=len(orders)))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Будь-яка помилка: менеджер отримує подробиці, клієнт — коротке вибачення."""
    logging.error("Помилка під час обробки оновлення", exc_info=context.error)
    manager = manager_id(context)
    chat = update.effective_chat if isinstance(update, Update) else None
    if chat and chat.id != manager:
        try:
            await context.bot.send_message(chat.id, texts.ERROR_FOR_USER)
        except Exception:
            logging.exception("Не вдалося повідомити клієнта про помилку")
    if manager:
        trace = "".join(traceback.format_exception(context.error))[-3000:]
        where = f"Чат: {chat.id}\n" if chat else ""
        try:
            await context.bot.send_message(
                manager, f"{texts.ERROR_ALERT}\n{html.escape(where)}<pre>{html.escape(trace)}</pre>",
                parse_mode=ParseMode.HTML)
        except Exception:
            logging.exception("Не вдалося надіслати менеджеру повідомлення про помилку")


# ---------- Запуск ----------

async def set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Головне меню"),
        BotCommand("catalog", "Каталог товарів"),
        BotCommand("cart", "Кошик"),
        BotCommand("myorders", "Мої замовлення"),
        BotCommand("logo", "Приміряти логотип"),
        BotCommand("cancel", "Скасувати оформлення"),
    ])


def button(text: str) -> filters.BaseFilter:
    return filters.Regex(f"^{re.escape(text)}$")


def build_app(token: str, db_path: Path | str, request=None) -> Application:
    builder = Application.builder().token(token).post_init(set_commands)
    if request:
        builder = builder.request(request)
    app = builder.build()
    app.bot_data["db"] = Storage(db_path)

    cancel = button(BTN_CANCEL)
    answer = filters.TEXT & ~filters.COMMAND & ~cancel
    # Розмова починається з inline-кнопки, а далі йде звичайними повідомленнями —
    # попередження PTB про per_message тут не стосується нашого випадку.
    warnings.filterwarnings("ignore", message=".*per_message.*", category=PTBUserWarning)
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(checkout_start, pattern="^checkout$")],
        states={
            REUSE: [MessageHandler(answer, order_reuse)],
            NAME: [MessageHandler(answer, order_name)],
            PHONE: [MessageHandler(filters.CONTACT | answer, order_phone)],
            DELIVERY_METHOD: [MessageHandler(answer, order_delivery_method)],
            DELIVERY: [MessageHandler(answer, order_delivery)],
            PAYMENT: [MessageHandler(answer, order_payment)],
            COMMENT: [MessageHandler(answer, order_comment)],
            CONFIRM: [MessageHandler(button(BTN_CONFIRM), order_confirm)],
        },
        fallbacks=[MessageHandler(cancel, order_cancel), CommandHandler("cancel", order_cancel)],
    ))

    app.add_handler(CallbackQueryHandler(catalog_action,
                                         pattern=r"^(cats|hits|c:\d+|m:\w+|[vs]:\w+:[^:]*|chart:\w+)$"))
    app.add_handler(CallbackQueryHandler(rate_order, pattern=r"^rate:\d+:[1-5]$"))
    app.add_handler(CallbackQueryHandler(ask_manager, pattern=r"^ask:\w+:[^:]*$"))
    app.add_handler(CallbackQueryHandler(cart_action, pattern=r"^(add|inc|dec):\w+(:[^:]+)?$|^(show|clear|noop)$"))
    app.add_handler(CallbackQueryHandler(logo_action, pattern=r"^lg[cswnmo]?(:\w+)?$"))
    app.add_handler(CallbackQueryHandler(manager_open_order, pattern=r"^ord:\d+$"))
    app.add_handler(CallbackQueryHandler(manager_set_status, pattern=r"^st:\d+:\w+$"))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("catalog", catalog))
    app.add_handler(CommandHandler("cart", show_cart))
    app.add_handler(CommandHandler("myorders", my_orders))
    app.add_handler(CommandHandler("logo", logo_start))
    app.add_handler(CommandHandler("manager", become_manager))
    app.add_handler(CommandHandler("orderschat", set_orders_chat))
    app.add_handler(CommandHandler("orders", manager_orders))
    app.add_handler(CommandHandler("stats", manager_stats))
    app.add_handler(CommandHandler("report", manager_report))
    app.add_handler(CommandHandler("export", manager_export))
    app.add_handler(MessageHandler(button(BTN_CATALOG), catalog))
    app.add_handler(MessageHandler(button(BTN_CART), show_cart))
    app.add_handler(MessageHandler(button(BTN_DELIVERY), delivery))
    app.add_handler(MessageHandler(button(BTN_CONTACTS), contacts))
    app.add_handler(MessageHandler(button(BTN_MY_ORDERS), my_orders))
    app.add_handler(MessageHandler(button(BTN_LOGO), logo_start))
    app.add_handler(MessageHandler(button(BTN_CALL_MANAGER), call_manager))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.IMAGE),
                                   logo_received))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))
    app.add_error_handler(on_error)

    hour, minute = texts.REPORT_TIME
    app.job_queue.run_daily(daily_report_job, time(hour, minute, tzinfo=KYIV), name="daily_report")
    return app


def main() -> None:
    # Значення, вставлені у форму хостингу, часто мають зайвий перенос рядка наприкінці.
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or "..." in token:
        raise SystemExit("Вкажіть TELEGRAM_BOT_TOKEN у файлі .env (токен від @BotFather).")

    # DATABASE_URL — постійна база PostgreSQL (на Render диск очищається при кожному оновленні);
    # без неї замовлення зберігаються у файлі shop.db.
    app = build_app(token, os.getenv("DATABASE_URL", "").strip() or os.getenv("DB_PATH") or BASE / "shop.db")

    # На хостингу (Render сам задає RENDER_EXTERNAL_URL) працюємо через webhook, локально — через polling.
    public_url = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if public_url:
        logging.info("Бот запущено в режимі webhook: %s", public_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.getenv("PORT", "10000")),
            url_path="telegram",
            webhook_url=f"{public_url.rstrip('/')}/telegram",
            secret_token=hashlib.sha256(token.encode()).hexdigest()[:32],
        )
    else:
        logging.info("Бот запущено. Зупинити: Ctrl+C")
        app.run_polling()


if __name__ == "__main__":
    main()
