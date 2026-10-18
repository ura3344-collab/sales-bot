"""Каталог, кошик і розпізнавання тем — чиста логіка без Telegram."""
import csv
import re
from dataclasses import dataclass, replace
from pathlib import Path

import texts

PRODUCTS_FILE = Path(__file__).parent / "products.csv"
PHOTOS_DIR = Path(__file__).parent / "photos"
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp")
MAX_QTY = 20


@dataclass(frozen=True)
class Product:
    """Один варіант товару: модель певного кольору. У кошику — ще й певного розміру (size)."""
    id: str
    category: str
    model: str
    color: str
    price: int
    in_stock: bool
    description: str
    on_request: bool = False          # «під замовлення»: купується лише через менеджера
    sizes: tuple[str, ...] = ()       # усі розміри моделі, порожньо — розмір не потрібен
    request_sizes: frozenset[str] = frozenset()  # розміри, що шиються під замовлення
    size_chart: str = ""              # файл у photos/charts/
    size: str = ""

    @property
    def title(self) -> str:
        details = ", ".join(x for x in (self.color.lower(), self.size) if x)
        return f"{self.model} ({details})" if details else self.model

    def needs_manager(self, size: str = "") -> bool:
        return self.on_request or size in self.request_sizes

    def can_buy(self, size: str = "") -> bool:
        """Чи можна покласти в кошик: є в наявності, не під замовлення, розмір вибрано правильно."""
        size_ok = size in self.sizes if self.sizes else not size
        return self.in_stock and size_ok and not self.needs_manager(size)


def load_products(path: Path | None = None) -> dict[str, Product]:
    # Файл читається щоразу, тож зміни в products.csv діють без перезапуску бота.
    with open(path or PRODUCTS_FILE, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    products = {}
    for r in rows:
        # Розміри через пробіл; зірочка позначає розмір під замовлення: "XS* S M L XL 2XL 3XL*".
        sizes = (r.get("розміри") or "").split()
        availability = r["в_наявності"].strip().lower()
        products[r["id"].strip()] = Product(
            id=r["id"].strip(),
            category=r["розділ"].strip(),
            model=r["модель"].strip(),
            color=r["колір"].strip(),
            price=int(r["ціна_грн"]),
            in_stock=availability == "так",
            description=r["опис"].strip(),
            on_request=availability == "під замовлення",
            sizes=tuple(s.rstrip("*") for s in sizes),
            request_sizes=frozenset(s.rstrip("*") for s in sizes if s.endswith("*")),
            size_chart=(r.get("розмірна_сітка") or "").strip(),
        )
    return products


def categories(products: dict[str, Product]) -> list[str]:
    """Розділи каталогу в порядку, в якому вони йдуть у products.csv."""
    return list(dict.fromkeys(p.category for p in products.values()))


def models(products: dict[str, Product], category: str) -> dict[str, list[Product]]:
    """Моделі розділу, а для кожної — її кольори."""
    result: dict[str, list[Product]] = {}
    for p in products.values():
        if p.category == category:
            result.setdefault(p.model, []).append(p)
    return result


def variants(products: dict[str, Product], product: Product) -> list[Product]:
    """Усі кольори тієї самої моделі."""
    return models(products, product.category)[product.model]


def photo_path(product_id: str, photos_dir: Path | None = None) -> Path | None:
    """Фото товару — файл photos/<id>.jpg (або .jpeg/.png/.webp). Немає файлу — немає фото."""
    for ext in PHOTO_EXTS:
        path = (photos_dir or PHOTOS_DIR) / f"{product_id}{ext}"
        if path.is_file():
            return path
    return None


def chart_path(product: Product) -> Path | None:
    """Розмірна сітка моделі — файл photos/charts/<розмірна_сітка з products.csv>."""
    path = PHOTOS_DIR / "charts" / product.size_chart
    return path if product.size_chart and path.is_file() else None


def cart_key(product_id: str, size: str = "") -> str:
    return f"{product_id}:{size}" if size else product_id


def parse_key(key: str) -> tuple[str, str]:
    """Ключ кошика "12:M" → ("12", "M"); без розміру — "12" → ("12", "")."""
    product_id, _, size = key.partition(":")
    return product_id, size


def cart_lines(cart: dict[str, int], products: dict[str, Product]) -> list[tuple[Product, int]]:
    """Позиції кошика, які досі можна купити. id позиції — ключ кошика, size — вибраний розмір."""
    lines = []
    for key, qty in cart.items():
        pid, size = parse_key(key)
        if qty > 0 and pid in products and products[pid].can_buy(size):
            lines.append((replace(products[pid], id=key, size=size), qty))
    return lines


def cart_total(lines: list[tuple[Product, int]]) -> int:
    return sum(p.price * qty for p, qty in lines)


def delivery_note(total: int) -> str:
    if total >= texts.FREE_DELIVERY_FROM:
        return "🚚 Доставка безкоштовна!"
    return f"🚚 До безкоштовної доставки залишилось {texts.FREE_DELIVERY_FROM - total} грн."


def _normalize(text: str) -> str:
    return text.lower().replace("’", "'").replace("ʼ", "'")


def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", _normalize(text))


def _stem(word: str) -> str:
    """Відкидає закінчення, щоб «білий» знаходив «Біла», а «бежевого» — «Бежевий»."""
    return word[:max(3, len(word) - 2)]


def search(products: dict[str, Product], text: str) -> list[Product]:
    """Товари, у розділі, назві чи кольорі яких є всі значущі слова запиту.
    Короткі слова, слова-запитання («ціна», «фартух») і SEARCH_STOP_WORDS не враховуються."""
    words = [w for w in _words(text) if len(w) >= 3 and w not in texts.SEARCH_STOP_WORDS
             and not any(w.startswith(kw) for kw in texts.KEYWORDS["prices"])]
    if not words:
        return []
    found = []
    for p in products.values():
        names = _words(f"{p.category} {p.model} {p.color}")
        if all(any(n.startswith(_stem(w)) for n in names) for w in words):
            found.append(p)
    return found


def hits(products: dict[str, Product]) -> list[Product]:
    """Хіти продажу з texts.HITS, яких немає в каталозі, пропускаються."""
    return [products[pid] for pid in texts.HITS if pid in products]


def find_topic(text: str) -> str | None:
    text = _normalize(text)
    words = _words(text)
    for topic, keywords in texts.KEYWORDS.items():
        for kw in keywords:
            if any(w.startswith(kw) for w in words) or (" " in kw and kw in text):
                return topic
    return None
