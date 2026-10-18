"""Зберігання замовлень і налаштувань: локально — у файлі SQLite, на хостингу — у PostgreSQL.
PostgreSQL вмикається, якщо замість шляху до файлу передати адресу бази postgresql://…"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")

# {pk} і {bigint} підставляються під конкретну базу: ID у Telegram не вміщаються у звичайний INTEGER PostgreSQL.
SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id {pk},
    created TEXT NOT NULL,
    user_id {bigint} NOT NULL,
    username TEXT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    delivery TEXT NOT NULL,
    payment TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    rating INTEGER
);
CREATE TABLE IF NOT EXISTS order_items (
    id {pk},
    order_id INTEGER NOT NULL REFERENCES orders(id),
    title TEXT NOT NULL,
    price INTEGER NOT NULL,
    qty INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
NEW_COLUMNS = {"payment": "TEXT NOT NULL DEFAULT ''", "comment": "TEXT NOT NULL DEFAULT ''", "rating": "INTEGER"}


def is_postgres(target: Path | str) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


class Storage:
    def __init__(self, target: Path | str):
        self.pg = is_postgres(target)
        if self.pg:
            self.url = str(target)
            with self._transaction() as conn:
                conn.execute(SCHEMA.format(pk="SERIAL PRIMARY KEY", bigint="BIGINT"))
                for column, definition in NEW_COLUMNS.items():
                    conn.execute(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {column} {definition}")
        else:
            self.conn = sqlite3.connect(target)
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript(SCHEMA.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT", bigint="INTEGER"))
            # Бази, створені раніше, отримують колонки, яких тоді ще не було.
            columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(orders)")}
            with self.conn:
                for column, definition in NEW_COLUMNS.items():
                    if column not in columns:
                        self.conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {definition}")

    @contextmanager
    def _transaction(self):
        """Усе всередині — одна транзакція. До PostgreSQL щоразу нове з'єднання:
        хмарна база закриває ті, що довго простоюють."""
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            with psycopg.connect(self.url, row_factory=dict_row) as conn:
                yield _Postgres(conn)
        else:
            with self.conn:
                yield self.conn

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._transaction() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._transaction() as conn:
            conn.execute(sql, params)

    def get_setting(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_setting(self, key: str, value: str) -> None:
        self._execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                      "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def create_order(self, user_id: int, username: str | None, name: str, phone: str, delivery: str,
                     items: list[tuple[str, int, int]], payment: str = "", comment: str = "") -> int:
        """items — список (назва, ціна, кількість). Повертає номер замовлення."""
        created = datetime.now(KYIV).strftime("%Y-%m-%d %H:%M")
        total = sum(price * qty for _, price, qty in items)
        sql = ("INSERT INTO orders (created, user_id, username, name, phone, delivery, payment, comment, total) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
        params = (created, user_id, username, name, phone, delivery, payment, comment, total)
        with self._transaction() as conn:
            if self.pg:
                order_id = conn.execute(sql + " RETURNING id", params).fetchone()["id"]
            else:
                order_id = conn.execute(sql, params).lastrowid
            for title, price, qty in items:
                conn.execute("INSERT INTO order_items (order_id, title, price, qty) VALUES (?, ?, ?, ?)",
                             (order_id, title, price, qty))
        return order_id

    def _orders(self, where: str = "", params: tuple = ()) -> list[dict]:
        """Замовлення разом із товарами — двома запитами на весь список, а не окремим на кожне."""
        with self._transaction() as conn:
            orders = [dict(r) for r in conn.execute(f"SELECT * FROM orders {where}", params).fetchall()]
            if not orders:
                return []
            ids = [o["id"] for o in orders]
            # У старих базах SQLite в order_items немає колонки id, але є вбудований rowid.
            position = "id" if self.pg else "rowid"
            items = conn.execute(
                f"SELECT order_id, title, price, qty FROM order_items "
                f"WHERE order_id IN ({', '.join('?' * len(ids))}) ORDER BY {position}", tuple(ids)).fetchall()
        by_order: dict[int, list[dict]] = {i: [] for i in ids}
        for item in items:
            item = dict(item)
            by_order[item.pop("order_id")].append(item)
        for order in orders:
            order["items"] = by_order[order["id"]]
        return orders

    def get_order(self, order_id: int) -> dict | None:
        orders = self._orders("WHERE id = ?", (order_id,))
        return orders[0] if orders else None

    def user_orders(self, user_id: int, limit: int = 5) -> list[dict]:
        return self._orders("WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))

    def last_contacts(self, user_id: int) -> dict | None:
        """Ім'я, телефон і доставка з останнього замовлення клієнта — щоб не вводити їх знову."""
        rows = self._query(
            "SELECT name, phone, delivery FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
        return rows[0] if rows else None

    def recent_orders(self, limit: int = 10) -> list[dict]:
        return self._orders("ORDER BY id DESC LIMIT ?", (limit,))

    def orders_since(self, since: datetime) -> list[dict]:
        """Замовлення, створені починаючи з since (за київським часом), від старих до нових."""
        return self._orders("WHERE created >= ? ORDER BY id", (since.strftime("%Y-%m-%d %H:%M"),))

    def all_orders(self) -> list[dict]:
        return self._orders("ORDER BY id")

    def set_status(self, order_id: int, status: str) -> None:
        self._execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))

    def set_rating(self, order_id: int, rating: int) -> None:
        self._execute("UPDATE orders SET rating = ? WHERE id = ?", (rating, order_id))

    def stats(self) -> dict:
        """Виручка й топ товарів рахуються без скасованих замовлень."""
        with self._transaction() as conn:
            by_status = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM orders GROUP BY status").fetchall()}
            paid = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS revenue "
                                "FROM orders WHERE status != 'cancelled'").fetchone()
            top = conn.execute(
                "SELECT i.title AS title, SUM(i.qty) AS qty FROM order_items i JOIN orders o ON o.id = i.order_id "
                "WHERE o.status != 'cancelled' GROUP BY i.title ORDER BY qty DESC, i.title LIMIT 3").fetchall()
            rated = conn.execute("SELECT COUNT(rating) AS n, AVG(rating) AS avg FROM orders "
                                 "WHERE rating IS NOT NULL").fetchone()
        revenue = int(paid["revenue"])
        return {
            "ratings": rated["n"],
            "avg_rating": round(float(rated["avg"]), 1) if rated["n"] else 0,
            "orders": sum(by_status.values()),
            "by_status": by_status,
            "revenue": revenue,
            "average": round(revenue / paid["n"]) if paid["n"] else 0,
            "top": [(r["title"], int(r["qty"])) for r in top],
        }


class _Postgres:
    """Обгортка, щоб той самий SQL з «?» працював і в PostgreSQL, де параметри позначаються «%s»."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql: str, params: tuple = ()):
        return self.conn.execute(sql.replace("?", "%s"), params or None)
