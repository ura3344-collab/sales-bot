import pytest

from db import Storage


@pytest.fixture
def db(tmp_path):
    return Storage(tmp_path / "test.db")


def make_order(db, user_id=100, items=(("Футболка", 450, 2), ("Худі", 1290, 1))):
    return db.create_order(user_id, "olha", "Ольга", "0501234567", "Київ, №5", list(items))


def test_create_and_get_order(db):
    order_id = make_order(db)
    order = db.get_order(order_id)
    assert order["total"] == 2190
    assert order["status"] == "new"
    assert order["items"] == [{"title": "Футболка", "price": 450, "qty": 2},
                              {"title": "Худі", "price": 1290, "qty": 1}]
    assert db.get_order(999) is None


def test_user_sees_only_own_orders(db):
    first = make_order(db, user_id=100)
    make_order(db, user_id=200)
    second = make_order(db, user_id=100)
    assert [o["id"] for o in db.user_orders(100)] == [second, first]


def test_status_change(db):
    order_id = make_order(db)
    db.set_status(order_id, "shipped")
    assert db.get_order(order_id)["status"] == "shipped"


def test_stats_exclude_cancelled(db):
    make_order(db, items=[("Футболка", 450, 2)])
    make_order(db, items=[("Худі", 1290, 1), ("Футболка", 450, 1)])
    cancelled = make_order(db, items=[("Кепка", 390, 10)])
    db.set_status(cancelled, "cancelled")

    stats = db.stats()
    assert stats["orders"] == 3
    assert stats["by_status"] == {"new": 2, "cancelled": 1}
    assert stats["revenue"] == 900 + 1740
    assert stats["average"] == 1320
    assert stats["top"] == [("Футболка", 3), ("Худі", 1)]


def test_empty_stats(db):
    assert db.stats() == {"orders": 0, "by_status": {}, "revenue": 0, "average": 0, "top": [],
                          "ratings": 0, "avg_rating": 0}


def test_settings_survive_reopen(tmp_path):
    Storage(tmp_path / "s.db").set_setting("manager_id", "42")
    assert Storage(tmp_path / "s.db").get_setting("manager_id") == "42"
    assert Storage(tmp_path / "s.db").get_setting("missing") is None


def test_payment_comment_and_last_contacts(db):
    assert db.last_contacts(100) is None
    make_order(db)
    order_id = db.create_order(100, "olha", "Ольга К.", "0671112233", "Укрпошта, Львів", [("Кепка", 390, 1)],
                               payment="Накладений платіж", comment="Після 18:00")
    order = db.get_order(order_id)
    assert order["payment"] == "Накладений платіж" and order["comment"] == "Після 18:00"
    assert db.last_contacts(100) == {"name": "Ольга К.", "phone": "0671112233", "delivery": "Укрпошта, Львів"}


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, "
                 "user_id INTEGER NOT NULL, username TEXT, name TEXT NOT NULL, phone TEXT NOT NULL, "
                 "delivery TEXT NOT NULL, total INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'new')")
    conn.close()
    db = Storage(path)
    order_id = make_order(db)
    assert db.get_order(order_id)["payment"] == ""
