"""
Extractor Agent

Pulls structured product data out of HTML pages.

Strategy (in order of preference):
  1. JSON-LD — most reliable, machine-readable, already structured
  2. HTML selectors — fallback for detail pages where JSON-LD is sparse
  3. LLM extraction — last resort for pages with irregular layouts

For Safco Dental specifically, the listing pages embed rich JSON-LD
(ItemList with nested Product objects), so that's our primary path.
Product detail pages are JS-rendered (Magento + Hyvä), so we try
to grab what we can from raw HTML and note the rest as a limitation.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.config import LLMConfig
from scraper.models import ProductRecord
from scraper import llm

log = logging.getLogger(__name__)


class Extractor:
    def __init__(self, llm_cfg: LLMConfig, base_url: str):
        self.llm_cfg = llm_cfg
        self.base_url = base_url

    def extract_from_listing(self, html: str, category_path: str = "") -> list[ProductRecord]:
        """
        Extract products from a category/subcategory listing page.

        These pages embed an ItemList JSON-LD block with all visible products.
        This is our bread-and-butter extraction path.
        """
        soup = BeautifulSoup(html, "lxml")
        products = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw = json.loads(script.string)
            except (json.JSONDecodeError, TypeError):
                continue

            # JSON-LD can be a single object or an array of objects
            blocks = raw if isinstance(raw, list) else [raw]

            for data in blocks:
                if not isinstance(data, dict) or data.get("@type") != "ItemList":
                    continue

                for entry in data.get("itemListElement", []):
                    item = entry.get("item", {})
                    if item.get("@type") != "Product":
                        continue

                    product = self._parse_jsonld_product(item, entry.get("url", ""), category_path)
                    if product:
                        products.append(product)

        log.info(f"Extracted {len(products)} products from listing (JSON-LD)")
        return products

    def _parse_jsonld_product(
        self, item: dict, url: str, category_path: str
    ) -> Optional[ProductRecord]:
        """Parse a single Product object from JSON-LD."""

        name = item.get("name", "").strip()
        if not name:
            return None

        # Price / offer info
        offers = item.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        price = None
        price_raw = offers.get("price")
        if price_raw is not None:
            try:
                price = float(price_raw)
            except (ValueError, TypeError):
                pass

        availability_raw = offers.get("availability", "")
        availability = availability_raw.split("/")[-1] if availability_raw else None

        # Images
        images = item.get("image", [])
        if isinstance(images, str):
            images = [images]
        # Clean up image URLs — remove the resize params for full-size
        clean_images = []
        for img in images:
            # Strip ?width=... query params to get the original
            base_img = img.split("?")[0] if "?" in img else img
            clean_images.append(base_img)

        # Rating
        agg = item.get("aggregateRating", {})
        rating = None
        review_count = None
        if agg:
            try:
                rating = float(agg.get("ratingValue", 0))
                review_count = int(agg.get("reviewCount", 0))
            except (ValueError, TypeError):
                pass

        # Description — sometimes present in JSON-LD, often not on listings
        description = item.get("description") or None

        # Brand
        brand_info = item.get("brand", {})
        brand = brand_info.get("name") if isinstance(brand_info, dict) else str(brand_info) if brand_info else None

        return ProductRecord(
            product_name=name,
            brand=brand,
            sku=item.get("sku"),
            category_path=category_path,
            product_url=url or "",
            price=price,
            currency=offers.get("priceCurrency", "USD"),
            availability=availability,
            description=description,
            image_urls=clean_images,
            rating=rating,
            review_count=review_count,
        )

    def extract_from_detail_page(self, html: str, url: str, category_path: str = "") -> Optional[ProductRecord]:
        """
        Try to extract product data from a detail page.

        Safco's detail pages are JS-rendered (Magento + Hyvä theme with Alpine.js),
        so the main product content often isn't in the raw HTML. We try our best
        with what's available.
        """
        soup = BeautifulSoup(html, "lxml")

        # Try JSON-LD first (sometimes product pages have their own)
        product = self._try_detail_jsonld(soup, url, category_path)
        if product:
            return product

        # Try HTML meta tags and visible content
        product = self._try_detail_html(soup, url, category_path)
        if product:
            return product

        # LLM fallback
        product = self._try_detail_llm(html, url, category_path)
        return product

    def _try_detail_jsonld(self, soup: BeautifulSoup, url: str, category_path: str) -> Optional[ProductRecord]:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw = json.loads(script.string)
            except (json.JSONDecodeError, TypeError):
                continue

            blocks = raw if isinstance(raw, list) else [raw]
            for data in blocks:
                if isinstance(data, dict) and data.get("@type") == "Product":
                    return self._parse_jsonld_product(data, url, category_path)

        return None

    def _try_detail_html(self, soup: BeautifulSoup, url: str, category_path: str) -> Optional[ProductRecord]:
        """Scrape what we can from meta tags and page structure."""

        name = None
        # Try og:title or <title>
        og_title = soup.find("meta", property="og:title")
        if og_title:
            name = og_title.get("content", "").strip()
        if not name:
            title_tag = soup.find("title")
            if title_tag:
                name = title_tag.get_text(strip=True).split("|")[0].strip()

        if not name:
            return None

        # Try to find price in the page
        price = None
        price_el = soup.find("span", class_=re.compile(r"price"))
        if price_el:
            price_text = price_el.get_text(strip=True)
            match = re.search(r"[\d,.]+", price_text)
            if match:
                try:
                    price = float(match.group().replace(",", ""))
                except ValueError:
                    pass

        # SKU
        sku = None
        sku_el = soup.find(string=re.compile(r"SKU|Item\s*#|Product\s*Code", re.IGNORECASE))
        if sku_el:
            parent = sku_el.parent
            if parent:
                sku_text = parent.get_text(strip=True)
                match = re.search(r"[A-Z0-9]{3,}", sku_text)
                if match:
                    sku = match.group()

        # Description from meta
        desc = None
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc:
            desc = meta_desc.get("content", "").strip()

        # Image from og:image
        image_urls = []
        og_image = soup.find("meta", property="og:image")
        if og_image:
            image_urls.append(og_image.get("content", ""))

        return ProductRecord(
            product_name=name,
            sku=sku,
            category_path=category_path,
            product_url=url,
            price=price,
            description=desc,
            image_urls=image_urls,
        )

    def _try_detail_llm(self, html: str, url: str, category_path: str) -> Optional[ProductRecord]:
        """Ask the LLM to extract product data from raw HTML."""
        if not llm.llm_available():
            return None

        # Send a trimmed version — we don't need the full page
        snippet = html[:5000]

        prompt = (
            "Extract product information from this HTML page. "
            "Return a JSON object with these fields (use null for missing):\n"
            "  product_name, brand, sku, price (number), availability, "
            "  description, unit_pack_size, specifications (object), "
            "  image_urls (array of strings)\n\n"
            f"URL: {url}\n\n"
            f"HTML:\n{snippet}"
        )
        system = (
            "You are a data extraction assistant. "
            "Return ONLY valid JSON, no markdown fences, no explanation."
        )

        result = llm.ask_json(prompt, self.llm_cfg, system=system)
        if not result:
            return None

        try:
            return ProductRecord(
                product_name=result.get("product_name", "Unknown"),
                brand=result.get("brand"),
                sku=result.get("sku"),
                category_path=category_path,
                product_url=url,
                price=float(result["price"]) if result.get("price") else None,
                availability=result.get("availability"),
                description=result.get("description"),
                unit_pack_size=result.get("unit_pack_size"),
                specifications=result.get("specifications", {}),
                image_urls=result.get("image_urls", []),
            )
        except Exception:
            log.warning(f"LLM extraction returned unparseable data for {url}")
            return None
