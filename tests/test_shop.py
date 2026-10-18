import shop
import texts

CSV = (
    "id,розділ,модель,колір,ціна_грн,в_наявності,опис\n"
    "1,Одяг,Футболка,Біла,450,так,Бавовна\n"
    "2,Одяг,Худі,,1290,Так,Тринитка\n"
    "3,Шапки,Кепка,,390,ні,Очікується\n"
    "4,Одяг,Футболка,Чорна,450,так,Бавовна\n"
)


def products(tmp_path):
    path = tmp_path / "products.csv"
    path.write_text(CSV, encoding="utf-8-sig")
    return shop.load_products(path)


def test_load_products(tmp_path):
    items = products(tmp_path)
    assert list(items) == ["1", "2", "3", "4"]
    assert items["1"] == shop.Product("1", "Одяг", "Футболка", "Біла", 450, True, "Бавовна")
    assert items["1"].title == "Футболка (біла)"
    assert items["2"].title == "Худі"
    assert items["2"].in_stock
    assert not items["3"].in_stock


def test_real_catalog_is_valid():
    items = shop.load_products()
    assert items
    assert all(p.price > 0 and p.title for p in items.values())
    # Для кожного товару справжнього каталогу є фото в photos/.
    assert [pid for pid in items if not shop.photo_path(pid)] == []
    assert [p.id for p in items.values() if p.size_chart and not shop.chart_path(p)] == []


def test_categories_models_and_variants(tmp_path):
    items = products(tmp_path)
    assert shop.categories(items) == ["Одяг", "Шапки"]
    models = shop.models(items, "Одяг")
    assert list(models) == ["Футболка", "Худі"]
    assert [p.color for p in models["Футболка"]] == ["Біла", "Чорна"]
    assert [p.id for p in shop.variants(items, items["4"])] == ["1", "4"]


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


def test_search_by_name_and_color(tmp_path):
    items = products(tmp_path)
    # Закінчення не важливі: «білу» знаходить «Біла», «футболки» — «Футболка».
    assert [p.id for p in shop.search(items, "біла футболка")] == ["1"]
    assert [p.id for p in shop.search(items, "Є у вас футболки?")] == ["1", "4"]
    assert [p.id for p in shop.search(items, "скільки коштує худі")] == ["2"]
    # Без значущих слів або без збігу — нічого.
    assert shop.search(items, "ціна") == []
    assert shop.search(items, "зелена футболка") == []


def test_hits_skip_missing_products(tmp_path, monkeypatch):
    monkeypatch.setattr(texts, "HITS", ["4", "99", "2"])
    assert [p.id for p in shop.hits(products(tmp_path))] == ["4", "2"]


def test_real_hits_exist():
    items = shop.load_products()
    assert [p.id for p in shop.hits(items)] == texts.HITS
