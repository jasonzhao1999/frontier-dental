"""
SQLite storage + CSV/JSON export.

We use SQLite as the primary store because it gives us:
  - atomic inserts (safe for resume)
  - dedup via UNIQUE constraints
  - queryability for debugging
  - zero external deps (it ships with Python)
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from scraper.models import ProductRecord

log = logging.getLogger(__name__)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE,
    product_name TEXT NOT NULL,
    brand TEXT,
    sku TEXT,
    category_path TEXT,
    product_url TEXT,
    price REAL,
    currency TEXT DEFAULT 'USD',
    unit_pack_size TEXT,
    availability TEXT,
    description TEXT,
    specifications TEXT,
    image_urls TEXT,
    alternative_products TEXT,
    rating REAL,
    review_count INTEGER,
    scraped_at TEXT
);
"""

CREATE_CHECKPOINT = """
CREATE TABLE IF NOT EXISTS checkpoint (
    url TEXT PRIMARY KEY,
    status TEXT DEFAULT 'visited',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


class Storage:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(CREATE_TABLE)
        self.conn.execute(CREATE_CHECKPOINT)
        self.conn.commit()

    def save_product(self, product: ProductRecord) -> bool:
        """Insert a product. Returns True if it was new, False if duplicate."""
        try:
            self.conn.execute(
                """INSERT INTO products
                   (fingerprint, product_name, brand, sku, category_path,
                    product_url, price, currency, unit_pack_size, availability,
                    description, specifications, image_urls, alternative_products,
                    rating, review_count, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    product.fingerprint,
                    product.product_name,
                    product.brand,
                    product.sku,
                    product.category_path,
                    product.product_url,
                    product.price,
                    product.currency,
                    product.unit_pack_size,
                    product.availability,
                    product.description,
                    json.dumps(product.specifications),
                    json.dumps(product.image_urls),
                    json.dumps(product.alternative_products),
                    product.rating,
                    product.review_count,
                    product.scraped_at,
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            log.debug(f"Duplicate skipped: {product.sku} — {product.product_name}")
            return False

    def save_products(self, products: list[ProductRecord]) -> int:
        """Batch insert. Returns count of newly inserted rows."""
        new = 0
        for p in products:
            if self.save_product(p):
                new += 1
        return new

    def mark_visited(self, url: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO checkpoint (url) VALUES (?)", (url,)
        )
        self.conn.commit()

    def is_visited(self, url: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM checkpoint WHERE url = ?", (url,)
        ).fetchone()
        return row is not None

    def product_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM products").fetchone()
        return row[0]

    def all_products(self) -> list[dict]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute("SELECT * FROM products ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
        self.conn.row_factory = None
        return rows

    def export_json(self, path: str):
        rows = self.all_products()
        # Deserialize JSON string columns back to lists/dicts
        for r in rows:
            for col in ("specifications", "image_urls", "alternative_products"):
                if r[col]:
                    r[col] = json.loads(r[col])
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        log.info(f"Exported {len(rows)} products to {out}")

    def export_csv(self, path: str):
        rows = self.all_products()
        if not rows:
            log.warning("No products to export.")
            return
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Flatten JSON columns to strings for CSV
        fieldnames = [k for k in rows[0].keys() if k != "id"]
        with open(out, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                r.pop("id", None)
                writer.writerow(r)
        log.info(f"Exported {len(rows)} products to {out}")

    def close(self):
        self.conn.close()
