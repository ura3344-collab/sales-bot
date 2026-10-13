"""Зберігання замовлень і налаштувань у SQLite."""
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    delivery TEXT NOT NULL,
    total INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS order_items (
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


class Storage:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

    def create_order(self, user_id: int, username: str | None, name: str, phone: str, delivery: str,
                     items: list[tuple[str, int, int]]) -> int:
        """items — список (назва, ціна, кількість). Повертає номер замовлення."""
        created = datetime.now(KYIV).strftime("%Y-%m-%d %H:%M")
        total = sum(price * qty for _, price, qty in items)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO orders (created, user_id, username, name, phone, delivery, total) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (created, user_id, username, name, phone, delivery, total))
            self.conn.executemany(
                "INSERT INTO order_items (order_id, title, price, qty) VALUES (?, ?, ?, ?)",
                [(cur.lastrowid, title, price, qty) for title, price, qty in items])
        return cur.lastrowid

    def _with_items(self, row: sqlite3.Row) -> dict:
        order = dict(row)
        items = self.conn.execute(
            "SELECT title, price, qty FROM order_items WHERE order_id = ? ORDER BY rowid", (row["id"],))
        order["items"] = [dict(i) for i in items]
        return order

    def get_order(self, order_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return self._with_items(row) if row else None

    def user_orders(self, user_id: int, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return [self._with_items(r) for r in rows.fetchall()]

    def recent_orders(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
        return [self._with_items(r) for r in rows.fetchall()]

    def set_status(self, order_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))

    def stats(self) -> dict:
        """Виручка й топ товарів рахуються без скасованих замовлень."""
        by_status = dict(self.conn.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall())
        paid_count, revenue = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE status != 'cancelled'").fetchone()
        top = self.conn.execute(
            "SELECT i.title, SUM(i.qty) AS qty FROM order_items i JOIN orders o ON o.id = i.order_id "
            "WHERE o.status != 'cancelled' GROUP BY i.title ORDER BY qty DESC, i.title LIMIT 3").fetchall()
        return {
            "orders": sum(by_status.values()),
            "by_status": by_status,
            "revenue": revenue,
            "average": round(revenue / paid_count) if paid_count else 0,
            "top": [(r["title"], r["qty"]) for r in top],
        }
