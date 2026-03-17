"""
Data models for the scraper pipeline.

Everything flows through these — agents produce them, storage consumes them.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, computed_field


class PageType(str, Enum):
    CATEGORY = "category"
    LISTING = "listing"
    PRODUCT = "product"
    UNKNOWN = "unknown"


class Category(BaseModel):
    name: str
    url: str
    parent: Optional[str] = None
    depth: int = 0


class ProductRecord(BaseModel):
    """Normalized product record — this is what gets stored."""

    product_name: str
    brand: Optional[str] = None
    sku: Optional[str] = None
    category_path: str = ""
    product_url: str
    price: Optional[float] = None
    currency: str = "USD"
    unit_pack_size: Optional[str] = None
    availability: Optional[str] = None
    description: Optional[str] = None
    specifications: dict = Field(default_factory=dict)
    image_urls: list[str] = Field(default_factory=list)
    alternative_products: list[str] = Field(default_factory=list)
    rating: Optional[float] = None
    review_count: Optional[int] = None
    scraped_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    @computed_field
    @property
    def fingerprint(self) -> str:
        """
        Dedup key. Products with the same URL + SKU combo
        are treated as duplicates.
        """
        raw = f"{self.product_url}|{self.sku or ''}"
        return hashlib.md5(raw.encode()).hexdigest()


class ScrapedPage(BaseModel):
    """Intermediate result from fetching a page."""

    url: str
    status_code: int
    html: str
    page_type: PageType = PageType.UNKNOWN
    fetched_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class CrawlCheckpoint(BaseModel):
    """Tracks what we've already visited so we can resume."""

    visited_urls: set[str] = Field(default_factory=set)
    pending_urls: list[str] = Field(default_factory=list)
    product_count: int = 0
    last_updated: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
