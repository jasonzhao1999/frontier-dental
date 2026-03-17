"""
Navigator Agent

Responsible for discovering the crawl frontier:
  - Starts from seed category URLs
  - Discovers subcategories (links within /catalog/ paths)
  - Tracks pagination if present
  - Yields URLs for the orchestrator to process

Does NOT extract product data — that's the extractor's job.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.config import AppConfig
from scraper.fetcher import Fetcher
from scraper.models import Category

log = logging.getLogger(__name__)


class Navigator:
    def __init__(self, cfg: AppConfig, fetcher: Fetcher):
        self.cfg = cfg
        self.fetcher = fetcher
        self.base = cfg.target.base_url.rstrip("/")

    async def discover_subcategories(self, category_url: str, parent_name: str = "") -> list[Category]:
        """
        Fetch a category page and pull out subcategory links.

        Two strategies:
          1. Parse <a> tags whose href is a direct child of the category path.
          2. Look for category-style links in the JSON-LD BreadcrumbList or
             in structured navigation elements.
        """
        html, status = await self.fetcher.get_text(category_url)
        if status != 200:
            log.warning(f"Got {status} for {category_url}, skipping subcategory discovery")
            return []

        soup = BeautifulSoup(html, "lxml")
        # Normalize: "/catalog/sutures-surgical-products" (no trailing slash)
        path_prefix = category_url.replace(self.base, "").rstrip("/")
        subcategories = []
        seen = set()

        # Strategy 1: find <a> tags linking to child catalog paths
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()

            if href.startswith("/"):
                full_url = self.base + href.rstrip("/")
            elif href.startswith("http"):
                full_url = href.rstrip("/")
            else:
                continue

            # Normalize to just the path portion
            if self.base in full_url:
                full_path = full_url.replace(self.base, "")
            else:
                continue

            # Check: is this a direct child of the category?
            # e.g. path_prefix = "/catalog/gloves"
            #      full_path   = "/catalog/gloves/nitrile-gloves" -> yes
            #      full_path   = "/catalog/gloves" -> no (same page)
            #      full_path   = "/catalog/gloves/nitrile-gloves/extra" -> no (too deep)
            if not full_path.startswith(path_prefix + "/"):
                continue
            remainder = full_path[len(path_prefix) + 1:]
            if "/" in remainder or not remainder:
                continue
            if "/product/" in full_path:
                continue

            if full_url in seen:
                continue

            name = a.get_text(strip=True) or remainder.replace("-", " ").title()
            subcategories.append(
                Category(
                    name=name,
                    url=full_url,
                    parent=parent_name or path_prefix.split("/")[-1],
                    depth=1,
                )
            )
            seen.add(full_url)

        # Strategy 2: if nothing from HTML, try mining JSON-LD for category references
        if not subcategories:
            subcategories = self._subcategories_from_jsonld(soup, path_prefix, parent_name, seen)

        log.info(f"Found {len(subcategories)} subcategories under {category_url}")
        for sc in subcategories:
            log.info(f"  -> {sc.name}: {sc.url}")
        return subcategories

    def _subcategories_from_jsonld(
        self, soup: BeautifulSoup, path_prefix: str, parent_name: str, seen: set
    ) -> list[Category]:
        """
        Safco's Hyvä theme renders subcategory links into JSON-LD as
        CollectionPage items inside an ItemList. They have an @id like
        "...#subcategory-list" which makes them easy to identify.
        """
        results = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw = json.loads(script.string)
            except (json.JSONDecodeError, TypeError):
                continue

            blocks = raw if isinstance(raw, list) else [raw]
            for data in blocks:
                if not isinstance(data, dict) or data.get("@type") != "ItemList":
                    continue

                # Skip product lists — we only want subcategory lists
                list_id = data.get("@id", "")
                elements = data.get("itemListElement", [])

                # Check if this is a subcategory list by looking at item types
                # Subcategories use CollectionPage, products use Product
                for entry in elements:
                    item = entry.get("item", {})
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("@type", "")

                    if item_type in ("CollectionPage", "Category"):
                        url = item.get("url", "")
                        name = item.get("name", "")
                        if url and url not in seen:
                            results.append(
                                Category(name=name, url=url.rstrip("/"), parent=parent_name, depth=1)
                            )
                            seen.add(url)

                # If we found subcategories from this block, don't check others
                if results:
                    break

        return results

    async def find_pagination_urls(self, html: str, base_url: str) -> list[str]:
        """
        Look for pagination links on a listing page.

        Safco doesn't seem to paginate subcategory pages (they show all items),
        but we handle it in case they do for larger categories.
        """
        soup = BeautifulSoup(html, "lxml")
        pages = []
        seen = set()

        # Common Magento pagination patterns
        for a in soup.select("a.page-link, a.next, ul.pages a, .toolbar a"):
            href = a.get("href", "")
            if href and "p=" in href and href not in seen:
                full = urljoin(base_url, href)
                pages.append(full)
                seen.add(href)

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "?p=" in href or "&p=" in href:
                full = urljoin(base_url, href)
                if full not in seen:
                    pages.append(full)
                    seen.add(full)

        if pages:
            log.info(f"Found {len(pages)} pagination URLs on {base_url}")
        return pages

    async def get_product_urls_from_listing(self, html: str) -> list[str]:
        """
        Pull individual product page URLs from a listing page.
        These come from the JSON-LD ItemList data.
        """
        soup = BeautifulSoup(html, "lxml")
        urls = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw = json.loads(script.string)
            except (json.JSONDecodeError, TypeError):
                continue

            blocks = raw if isinstance(raw, list) else [raw]
            for data in blocks:
                if not isinstance(data, dict):
                    continue
                if data.get("@type") == "ItemList":
                    for item in data.get("itemListElement", []):
                        url = item.get("url")
                        if url:
                            urls.append(url)

        return urls
