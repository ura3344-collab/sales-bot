"""Каталог, кошик і розпізнавання тем — чиста логіка без Telegram."""
import csv
import re
from dataclasses import dataclass
from pathlib import Path

import texts

PRODUCTS_FILE = Path(__file__).parent / "products.csv"
PHOTOS_DIR = Path(__file__).parent / "photos"
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp")
MAX_QTY = 20


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    price: int
    in_stock: bool
    description: str


def load_products(path: Path | None = None) -> dict[str, Product]:
    # Файл читається щоразу, тож зміни в products.csv діють без перезапуску бота.
    with open(path or PRODUCTS_FILE, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return {
        r["id"].strip(): Product(
            id=r["id"].strip(),
            title=r["назва"].strip(),
            price=int(r["ціна_грн"]),
            in_stock=r["в_наявності"].strip().lower() == "так",
            description=r["опис"].strip(),
        )
        for r in rows
    }


def photo_path(product_id: str, photos_dir: Path | None = None) -> Path | None:
    """Фото товару — файл photos/<id>.jpg (або .jpeg/.png/.webp). Немає файлу — немає фото."""
    for ext in PHOTO_EXTS:
        path = (photos_dir or PHOTOS_DIR) / f"{product_id}{ext}"
        if path.is_file():
            return path
    return None


def cart_lines(cart: dict[str, int], products: dict[str, Product]) -> list[tuple[Product, int]]:
    """Позиції кошика, які досі можна купити (товар існує і є в наявності)."""
    return [(products[pid], qty) for pid, qty in cart.items()
            if qty > 0 and pid in products and products[pid].in_stock]


def cart_total(lines: list[tuple[Product, int]]) -> int:
    return sum(p.price * qty for p, qty in lines)


def delivery_note(total: int) -> str:
    if total >= texts.FREE_DELIVERY_FROM:
        return "🚚 Доставка безкоштовна!"
    return f"🚚 До безкоштовної доставки залишилось {texts.FREE_DELIVERY_FROM - total} грн."


def find_topic(text: str) -> str | None:
    text = text.lower().replace("’", "'").replace("ʼ", "'")
    words = re.findall(r"[\w']+", text)
    for topic, keywords in texts.KEYWORDS.items():
        for kw in keywords:
            if any(w.startswith(kw) for w in words) or (" " in kw and kw in text):
                return topic
    return None
