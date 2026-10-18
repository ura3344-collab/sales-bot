import shop
import texts

CSV = (
    "id,назва,ціна_грн,в_наявності,опис\n"
    "1,Футболка,450,так,Бавовна\n"
    "2,Худі,1290,Так,Тринитка\n"
    "3,Кепка,390,ні,Очікується\n"
)


def products(tmp_path):
    path = tmp_path / "products.csv"
    path.write_text(CSV, encoding="utf-8-sig")
    return shop.load_products(path)


def test_load_products(tmp_path):
    items = products(tmp_path)
    assert list(items) == ["1", "2", "3"]
    assert items["1"] == shop.Product("1", "Футболка", 450, True, "Бавовна")
    assert items["2"].in_stock
    assert not items["3"].in_stock


def test_real_catalog_is_valid():
    items = shop.load_products()
    assert items
    assert all(p.price > 0 and p.title for p in items.values())
    # Для кожного товару справжнього каталогу є фото в photos/.
    assert [pid for pid in items if not shop.photo_path(pid)] == []


def test_cart_skips_unavailable_and_unknown(tmp_path):
    items = products(tmp_path)
    lines = shop.cart_lines({"1": 2, "3": 1, "99": 5, "2": 0}, items)
    assert [(p.id, qty) for p, qty in lines] == [("1", 2)]
    assert shop.cart_total(lines) == 900


def test_delivery_note():
    assert "безкоштовна" in shop.delivery_note(texts.FREE_DELIVERY_FROM)
    assert "100 грн" in shop.delivery_note(texts.FREE_DELIVERY_FROM - 100)


def test_find_topic():
    assert shop.find_topic("Скільки коштує худі?") == "prices"
    assert shop.find_topic("Як працює ДОСТАВКА") == "delivery"
    assert shop.find_topic("хочу купити футболку") == "order"
    assert shop.find_topic("як з вами зв’язатися") == "contacts"
    assert shop.find_topic("покажи кошик") == "cart"
    assert shop.find_topic("дякую!") == "thanks"
    assert shop.find_topic("абракадабра") is None
