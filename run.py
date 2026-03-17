#!/usr/bin/env python3
"""
Safco Dental product scraper — orchestrator / entry point.

This is the "brain" that coordinates all the agents:
  Navigator  -> discovers categories and product URLs
  Classifier -> decides what type of page we're looking at
  Extractor  -> pulls product data from pages
  Validator  -> cleans and deduplicates before storage

Usage:
    python run.py                    # scrape all configured categories
    python run.py --categories 1     # only scrape the first seed category
    python run.py --skip-details     # skip product detail pages (faster)
    python run.py --export-only      # just re-export from existing DB
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from urllib.parse import urljoin

from scraper.config import load_config, AppConfig
from scraper.fetcher import Fetcher
from scraper.navigator import Navigator
from scraper.classifier import classify
from scraper.extractor import Extractor
from scraper.validator import Validator
from scraper.storage import Storage
from scraper.models import PageType


def setup_logging(cfg: AppConfig):
    log_path = Path(cfg.logging.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(log_path), encoding="utf-8"),
    ]

    logging.basicConfig(
        level=getattr(logging, cfg.logging.level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


async def scrape_category(
    category_url: str,
    nav: Navigator,
    extractor: Extractor,
    validator: Validator,
    storage: Storage,
    fetcher: Fetcher,
    cfg: AppConfig,
    skip_details: bool = False,
):
    """Process one top-level category end-to-end."""
    log = logging.getLogger("orchestrator")

    category_name = category_url.rstrip("/").split("/")[-1]
    log.info(f"=== Processing category: {category_name} ===")

    # Step 1: discover subcategories
    subcategories = await nav.discover_subcategories(category_url, parent_name=category_name)

    # If no subcategories found, treat the category page itself as a listing
    if not subcategories:
        log.info(f"No subcategories found, treating {category_url} as a listing page")
        await scrape_listing_page(
            category_url, category_name, extractor, validator, storage, fetcher, cfg, skip_details
        )
        return

    # Step 2: scrape each subcategory
    for subcat in subcategories:
        if storage.is_visited(subcat.url):
            log.info(f"Skipping already-visited subcategory: {subcat.name}")
            continue

        category_path = f"{category_name} > {subcat.name}"
        await scrape_listing_page(
            subcat.url, category_path, extractor, validator, storage, fetcher, cfg, skip_details
        )
        storage.mark_visited(subcat.url)


async def scrape_listing_page(
    url: str,
    category_path: str,
    extractor: Extractor,
    validator: Validator,
    storage: Storage,
    fetcher: Fetcher,
    cfg: AppConfig,
    skip_details: bool,
):
    """Scrape a single listing page (subcategory) and optionally its product detail pages."""
    log = logging.getLogger("orchestrator")

    html, status = await fetcher.get_text(url)
    if status != 200:
        log.error(f"Failed to fetch listing page {url} (status {status})")
        return

    # Classify to make sure it's actually a listing
    page_type = classify(html, url, cfg.llm)
    if page_type not in (PageType.LISTING, PageType.CATEGORY):
        log.warning(f"Expected listing page but got {page_type.value} for {url}")

    # Extract products from the listing page JSON-LD
    products = extractor.extract_from_listing(html, category_path)

    if not products:
        log.warning(f"No products found on {url}")
        return

    # Validate and store
    clean_products = validator.validate_batch(products)
    new_count = storage.save_products(clean_products)
    log.info(f"Stored {new_count} new products from {url}")

    if skip_details:
        return

    # Step 3: optionally visit product detail pages for extra data
    # (description, specs, alternatives)
    nav = Navigator(cfg, fetcher)
    product_urls = await nav.get_product_urls_from_listing(html)

    for product_url in product_urls:
        if storage.is_visited(product_url):
            continue

        try:
            detail_html, detail_status = await fetcher.get_text(product_url)
            if detail_status != 200:
                continue

            detail_product = extractor.extract_from_detail_page(
                detail_html, product_url, category_path
            )

            if detail_product:
                # Merge detail data into the listing record
                _merge_detail_data(storage, detail_product)

            storage.mark_visited(product_url)

        except Exception:
            log.exception(f"Error scraping detail page {product_url}")
            continue


def _merge_detail_data(storage: Storage, detail: ProductRecord):
    """
    If we already have this product from the listing page, enrich it
    with any extra data from the detail page (description, specs, etc.).
    Otherwise just insert it.
    """
    log = logging.getLogger("orchestrator")

    # For now, just try to insert — the UNIQUE constraint on fingerprint
    # will prevent real duplicates. If we got new data (like description),
    # update the existing row.
    existing = storage.conn.execute(
        "SELECT id, description, specifications FROM products WHERE product_url = ?",
        (detail.product_url,),
    ).fetchone()

    if existing and detail.description and not existing[1]:
        storage.conn.execute(
            "UPDATE products SET description = ?, specifications = ? WHERE id = ?",
            (detail.description, str(detail.specifications), existing[0]),
        )
        storage.conn.commit()
        log.debug(f"Enriched product {detail.product_url} with detail page data")
    elif not existing:
        storage.save_product(detail)


async def main(args: argparse.Namespace):
    cfg = load_config(args.config)
    setup_logging(cfg)
    log = logging.getLogger("orchestrator")

    storage = Storage(cfg.storage.db_path)

    if args.export_only:
        log.info("Export-only mode — skipping scrape")
        storage.export_json(cfg.storage.export_json)
        storage.export_csv(cfg.storage.export_csv)
        log.info(f"Total products in DB: {storage.product_count()}")
        storage.close()
        return

    log.info("Starting Safco Dental scraper")
    log.info(f"Seed categories: {cfg.target.seed_categories}")

    validator = Validator()
    extractor = Extractor(cfg.llm, cfg.target.base_url)

    async with Fetcher(cfg.scraping) as fetcher:
        nav = Navigator(cfg, fetcher)

        seeds = cfg.target.seed_categories
        if args.categories:
            seeds = seeds[: args.categories]

        for seed in seeds:
            full_url = urljoin(cfg.target.base_url, seed)
            await scrape_category(
                full_url, nav, extractor, validator, storage, fetcher, cfg,
                skip_details=args.skip_details,
            )

    # Export
    storage.export_json(cfg.storage.export_json)
    storage.export_csv(cfg.storage.export_csv)

    log.info("--- Run complete ---")
    log.info(f"Total products in DB: {storage.product_count()}")
    log.info(validator.summary())

    storage.close()


def cli():
    parser = argparse.ArgumentParser(description="Safco Dental product scraper")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument(
        "--categories", type=int, default=None,
        help="Only scrape the first N seed categories",
    )
    parser.add_argument(
        "--skip-details", action="store_true",
        help="Skip fetching individual product detail pages (faster run)",
    )
    parser.add_argument(
        "--export-only", action="store_true",
        help="Skip scraping, just export existing DB to JSON/CSV",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = cli()
    asyncio.run(main(args))
