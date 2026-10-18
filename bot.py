"""Безкоштовний Telegram-бот магазину: каталог, кошик, замовлення, панель менеджера.
Запуск: python bot.py"""
import hashlib
import logging
import os
import re
import warnings
from pathlib import Path

from dotenv import load_dotenv
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton,
                      ReplyKeyboardMarkup, Update)
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          ConversationHandler, MessageHandler, filters)
from telegram.warnings import PTBUserWarning

import shop
import texts
from db import Storage

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

MENU = ReplyKeyboardMarkup([[BTN_CATALOG, BTN_CART], [BTN_DELIVERY, BTN_CONTACTS], [BTN_MY_ORDERS]],
                           resize_keyboard=True)
CANCEL_KB = ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)
PHONE_KB = ReplyKeyboardMarkup([[KeyboardButton("📱 Надіслати мій номер", request_contact=True)], [BTN_CANCEL]],
                               resize_keyboard=True)
CONFIRM_KB = ReplyKeyboardMarkup([[BTN_CONFIRM, BTN_CANCEL]], resize_keyboard=True)

NAME, PHONE, DELIVERY, CONFIRM = range(4)


def storage(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.bot_data["db"]


def get_cart(context: ContextTypes.DEFAULT_TYPE) -> dict[str, int]:
    return context.user_data.setdefault("cart", {})


def manager_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    # MANAGER_ID зі змінних середовища має пріоритет: на хостингу база може очищатися.
    value = os.getenv("MANAGER_ID", "").strip() or storage(context).get_setting("manager_id")
    return int(value) if value else None


def is_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return update.effective_chat.id == manager_id(context)


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
    return texts.CATALOG_TITLE, InlineKeyboardMarkup(buttons)


def models_view(products: dict[str, shop.Product], category: str) -> tuple[str, InlineKeyboardMarkup]:
    buttons = []
    for model, variants in shop.models(products, category).items():
        prices = {v.price for v in variants}
        price = f"{min(prices)} грн" if len(prices) == 1 else f"від {min(prices)} грн"
        buttons.append([InlineKeyboardButton(f"{model} — {price}", callback_data=f"m:{variants[0].id}")])
    buttons.append([InlineKeyboardButton(texts.BTN_ALL_CATEGORIES, callback_data="cats")])
    return texts.MODELS_TITLE.format(category=category), InlineKeyboardMarkup(buttons)


def product_caption(p: shop.Product) -> str:
    stock = "" if p.in_stock else f" ({texts.OUT_OF_STOCK})"
    color = f"\nКолір: {p.color}" if p.color else ""
    return f"{p.model} — {p.price} грн{stock}{color}\n\n{p.description}"


def product_markup(p: shop.Product, products: dict[str, shop.Product], cart: dict[str, int]) -> InlineKeyboardMarkup:
    rows = []
    variants = shop.variants(products, p)
    if len(variants) > 1:
        colors = [InlineKeyboardButton(f"✓ {v.color}" if v.id == p.id else v.color, callback_data=f"v:{v.id}")
                  for v in variants]
        rows += [colors[i:i + 3] for i in range(0, len(colors), 3)]
    row = []
    if p.in_stock:
        qty = cart.get(p.id, 0)
        row.append(InlineKeyboardButton(f"➕ Додати (у кошику: {qty})" if qty else "➕ Додати",
                                        callback_data=f"add:{p.id}"))
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
                         products: dict[str, shop.Product]) -> None:
    """Показує інший колір у тій самій картці: міняє фото, підпис і кнопки."""
    caption, markup = product_caption(p), product_markup(p, products, get_cart(context))
    path = shop.photo_path(p.id)
    if path and message.photo:
        key, photo = photo_input(context, path)
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


def order_card(order: dict) -> str:
    username = f"@{order['username']}" if order["username"] else "немає"
    items = items_text([(i["title"], i["price"], i["qty"]) for i in order["items"]])
    return (f"Замовлення №{order['id']} від {order['created']}\n"
            f"Статус: {texts.STATUSES[order['status']]}\n\n"
            f"{items}\nРазом: {order['total']} грн\n\n"
            f"Ім'я: {order['name']}\n"
            f"Телефон: {order['phone']}\n"
            f"Доставка: {order['delivery']}\n"
            f"Telegram: {username}")


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


async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(texts.CONTACTS, reply_markup=MENU)


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
    topic = shop.find_topic(update.message.text)
    if topic == "prices":
        await catalog(update, context)
        return
    if topic == "cart":
        await show_cart(update, context)
        return
    answers = {
        "delivery": texts.DELIVERY,
        "contacts": texts.CONTACTS,
        "order": texts.ORDER_HINT,
        "thanks": texts.THANKS,
        "greeting": texts.WELCOME,
    }
    await update.message.reply_text(answers.get(topic, texts.UNKNOWN), reply_markup=MENU)


# ---------- Каталог (inline-кнопки) ----------

async def catalog_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cats — список розділів, c:<№> — моделі розділу, m:<id> — картка моделі, v:<id> — інший колір."""
    query = update.callback_query
    action, _, arg = query.data.partition(":")
    products = shop.load_products()
    categories = shop.categories(products)
    if action == "cats":
        await query.answer()
        text, markup = categories_view(products)
        await edit_or_ignore(query.edit_message_text(text, reply_markup=markup))
    elif action == "c" and int(arg) < len(categories):
        await query.answer()
        text, markup = models_view(products, categories[int(arg)])
        await edit_or_ignore(query.edit_message_text(text, reply_markup=markup))
    elif action == "m" and arg in products:
        await query.answer()
        await send_product(query.message, context, products[arg], products)
    elif action == "v" and arg in products:
        await query.answer(products[arg].color)
        await switch_variant(query.message, context, products[arg], products)
    else:
        await query.answer(texts.CATALOG_CHANGED, show_alert=True)


# ---------- Кошик (inline-кнопки) ----------

async def cart_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, pid = query.data.partition(":")
    products = shop.load_products()
    cart = get_cart(context)
    available = pid in products and products[pid].in_stock

    if action == "noop":
        await query.answer()
    elif action == "show":
        await query.answer()
        text, markup = cart_view(cart, products)
        await query.message.reply_text(text, reply_markup=markup)
    elif action in ("add", "inc") and not available:
        await query.answer(texts.NOT_AVAILABLE, show_alert=True)
    elif action in ("add", "inc") and cart.get(pid, 0) >= shop.MAX_QTY:
        await query.answer(texts.MAX_QTY_REACHED, show_alert=True)
    elif action == "add":
        cart[pid] = cart.get(pid, 0) + 1
        await query.answer(f"Додано: {products[pid].title}")
        await edit_or_ignore(query.edit_message_reply_markup(product_markup(products[pid], products, cart)))
    else:
        if action == "inc":
            cart[pid] = cart.get(pid, 0) + 1
        elif action == "dec" and pid in cart:
            cart[pid] -= 1
            if cart[pid] <= 0:
                del cart[pid]
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
    await query.message.reply_text(texts.ASK_NAME, reply_markup=CANCEL_KB)
    return NAME


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
    await update.message.reply_text(texts.ASK_DELIVERY, reply_markup=CANCEL_KB)
    return DELIVERY


async def order_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order = context.user_data["order"]
    order["delivery"] = update.message.text
    lines = shop.cart_lines(get_cart(context), shop.load_products())
    total = shop.cart_total(lines)
    summary = (f"{texts.CHECK_ORDER}\n\n{items_text([(p.title, p.price, qty) for p, qty in lines])}\n"
               f"Разом: {total} грн\n{shop.delivery_note(total)}\n\n"
               f"Ім'я: {order['name']}\n"
               f"Телефон: {order['phone']}\n"
               f"Доставка: {order['delivery']}")
    await update.message.reply_text(summary, reply_markup=CONFIRM_KB)
    return CONFIRM


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
        [(p.title, p.price, qty) for p, qty in lines])
    cart.clear()

    manager = manager_id(context)
    if manager:
        try:
            await context.bot.send_message(
                manager, f"{texts.NEW_ORDER}\n\n{order_card(storage(context).get_order(order_id))}",
                reply_markup=status_keyboard(order_id))
        except Exception:
            logging.exception("Не вдалося надіслати замовлення менеджеру")
    else:
        logging.warning("Менеджера не призначено — замовлення №%s збережено лише в базі", order_id)

    await update.message.reply_text(texts.ORDER_DONE.format(id=order_id), reply_markup=MENU)
    return ConversationHandler.END


async def order_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("order", None)
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
    except Exception:
        logging.exception("Не вдалося сповістити клієнта про статус замовлення №%s", order["id"])


async def manager_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_manager(update, context):
        await update.message.reply_text(texts.MANAGER_ONLY)
        return
    s = storage(context).stats()
    lines = ["📊 Статистика", "",
             f"Замовлень: {s['orders']}",
             f"Виручка (без скасованих): {s['revenue']} грн",
             f"Середній чек: {s['average']} грн"]
    if s["by_status"]:
        lines += ["", "За статусами:"]
        lines += [f"{label}: {s['by_status'][status]}" for status, label in texts.STATUSES.items()
                  if status in s["by_status"]]
    if s["top"]:
        lines += ["", "Топ товарів:"]
        lines += [f"{n}. {title} — {qty} шт." for n, (title, qty) in enumerate(s["top"], 1)]
    await update.message.reply_text("\n".join(lines))


# ---------- Запуск ----------

async def set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Головне меню"),
        BotCommand("catalog", "Каталог товарів"),
        BotCommand("cart", "Кошик"),
        BotCommand("myorders", "Мої замовлення"),
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
            NAME: [MessageHandler(answer, order_name)],
            PHONE: [MessageHandler(filters.CONTACT | answer, order_phone)],
            DELIVERY: [MessageHandler(answer, order_delivery)],
            CONFIRM: [MessageHandler(button(BTN_CONFIRM), order_confirm)],
        },
        fallbacks=[MessageHandler(cancel, order_cancel), CommandHandler("cancel", order_cancel)],
    ))

    app.add_handler(CallbackQueryHandler(catalog_action, pattern=r"^(cats|c:\d+|m:\w+|v:\w+)$"))
    app.add_handler(CallbackQueryHandler(cart_action, pattern=r"^(add|inc|dec):\w+$|^(show|clear|noop)$"))
    app.add_handler(CallbackQueryHandler(manager_open_order, pattern=r"^ord:\d+$"))
    app.add_handler(CallbackQueryHandler(manager_set_status, pattern=r"^st:\d+:\w+$"))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("catalog", catalog))
    app.add_handler(CommandHandler("cart", show_cart))
    app.add_handler(CommandHandler("myorders", my_orders))
    app.add_handler(CommandHandler("manager", become_manager))
    app.add_handler(CommandHandler("orders", manager_orders))
    app.add_handler(CommandHandler("stats", manager_stats))
    app.add_handler(MessageHandler(button(BTN_CATALOG), catalog))
    app.add_handler(MessageHandler(button(BTN_CART), show_cart))
    app.add_handler(MessageHandler(button(BTN_DELIVERY), delivery))
    app.add_handler(MessageHandler(button(BTN_CONTACTS), contacts))
    app.add_handler(MessageHandler(button(BTN_MY_ORDERS), my_orders))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))
    return app


def main() -> None:
    # Значення, вставлені у форму хостингу, часто мають зайвий перенос рядка наприкінці.
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or "..." in token:
        raise SystemExit("Вкажіть TELEGRAM_BOT_TOKEN у файлі .env (токен від @BotFather).")

    app = build_app(token, os.getenv("DB_PATH") or BASE / "shop.db")

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
