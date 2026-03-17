"""
Validator Agent

Takes raw extracted products and cleans them up:
  - Drops records missing required fields
  - Normalizes text (strip whitespace, fix encoding artifacts)
  - Deduplicates by fingerprint
  - Flags suspicious data for review

This sits between the extractor and storage to catch garbage
before it hits the database.
"""

from __future__ import annotations

import logging
import re
from html import unescape

from scraper.models import ProductRecord

log = logging.getLogger(__name__)


class Validator:
    def __init__(self):
        self._seen_fingerprints: set[str] = set()
        self.stats = {"total": 0, "valid": 0, "duplicates": 0, "dropped": 0}

    def validate_batch(self, products: list[ProductRecord]) -> list[ProductRecord]:
        """Validate and deduplicate a batch of products."""
        clean = []
        for p in products:
            self.stats["total"] += 1
            result = self._validate_one(p)
            if result is None:
                continue
            if result.fingerprint in self._seen_fingerprints:
                self.stats["duplicates"] += 1
                log.debug(f"Duplicate in batch: {result.sku}")
                continue
            self._seen_fingerprints.add(result.fingerprint)
            self.stats["valid"] += 1
            clean.append(result)
        return clean

    def _validate_one(self, p: ProductRecord) -> ProductRecord | None:
        # Must have a name at minimum
        if not p.product_name or len(p.product_name.strip()) < 2:
            log.warning(f"Dropping product with empty/short name: {p.product_url}")
            self.stats["dropped"] += 1
            return None

        # Must have a URL
        if not p.product_url:
            log.warning(f"Dropping product with no URL: {p.product_name}")
            self.stats["dropped"] += 1
            return None

        # Normalize
        p = self._normalize(p)

        # Sanity-check price
        if p.price is not None and (p.price < 0 or p.price > 50000):
            log.warning(f"Suspicious price ${p.price} for {p.product_name}, clearing it")
            p.price = None

        return p

    def _normalize(self, p: ProductRecord) -> ProductRecord:
        """Clean up text fields."""
        p.product_name = self._clean_text(p.product_name)

        if p.brand:
            p.brand = self._clean_text(p.brand)

        if p.description:
            p.description = self._clean_text(p.description)

        if p.sku:
            p.sku = p.sku.strip().upper()

        if p.availability:
            # Normalize common variants
            avail = p.availability.lower().strip()
            if "instock" in avail or "in stock" in avail:
                p.availability = "In Stock"
            elif "outofstock" in avail or "out of stock" in avail:
                p.availability = "Out of Stock"
            elif "preorder" in avail:
                p.availability = "Pre-Order"

        # Deduplicate image URLs while keeping order
        if p.image_urls:
            seen = set()
            unique = []
            for url in p.image_urls:
                if url not in seen:
                    unique.append(url)
                    seen.add(url)
            p.image_urls = unique

        return p

    @staticmethod
    def _clean_text(text: str) -> str:
        text = unescape(text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def summary(self) -> str:
        return (
            f"Validation: {self.stats['total']} total, "
            f"{self.stats['valid']} valid, "
            f"{self.stats['duplicates']} duplicates, "
            f"{self.stats['dropped']} dropped"
        )
