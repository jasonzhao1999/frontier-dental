"""
Page Classifier Agent

Determines what kind of page we're looking at so the orchestrator
knows which extraction path to take.

Uses a rule-based approach first (fast, free). Falls back to the LLM
only when the heuristics aren't confident.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from scraper.config import LLMConfig
from scraper.models import PageType
from scraper import llm

log = logging.getLogger(__name__)


def classify(html: str, url: str, llm_cfg: LLMConfig) -> PageType:
    """Classify a page by URL pattern + HTML signals."""

    # Fast path: URL-based rules cover the majority of cases
    page_type = _classify_by_url(url)
    if page_type != PageType.UNKNOWN:
        log.debug(f"Classified {url} as {page_type.value} (url rule)")
        return page_type

    # Second pass: look at HTML structure
    page_type = _classify_by_html(html)
    if page_type != PageType.UNKNOWN:
        log.debug(f"Classified {url} as {page_type.value} (html rule)")
        return page_type

    # Last resort: ask the LLM
    page_type = _classify_by_llm(html, url, llm_cfg)
    log.debug(f"Classified {url} as {page_type.value} (llm)")
    return page_type


def _classify_by_url(url: str) -> PageType:
    path = url.rstrip("/").lower()

    if "/product/" in path:
        return PageType.PRODUCT

    if "/catalog/" in path:
        # /catalog/gloves -> could be category or listing
        # We'll refine this with HTML checks below
        segments = path.split("/catalog/")[1].split("/")
        if len(segments) >= 2:
            # e.g. /catalog/gloves/nitrile-gloves -> subcategory listing
            return PageType.LISTING
        else:
            # e.g. /catalog/gloves -> top-level category
            return PageType.CATEGORY

    return PageType.UNKNOWN


def _classify_by_html(html: str) -> PageType:
    soup = BeautifulSoup(html, "lxml")

    # JSON-LD with ItemList -> listing page
    scripts = soup.find_all("script", type="application/ld+json")
    for s in scripts:
        text = s.get_text()
        if '"ItemList"' in text:
            return PageType.LISTING
        if '"Product"' in text and '"ItemList"' not in text:
            return PageType.PRODUCT

    return PageType.UNKNOWN


def _classify_by_llm(html: str, url: str, llm_cfg: LLMConfig) -> PageType:
    if not llm.llm_available():
        return PageType.UNKNOWN

    # Only send a small chunk to keep token usage low
    snippet = html[:3000]
    prompt = (
        f"I'm scraping an e-commerce website. Given the URL and HTML snippet below, "
        f"classify this page as one of: category, listing, product, unknown.\n\n"
        f"URL: {url}\n\n"
        f"HTML snippet:\n{snippet}\n\n"
        f"Respond with a single word: category, listing, product, or unknown."
    )
    result = llm.ask(prompt, llm_cfg, system="You are a web scraping assistant. Respond with only the page type.")
    if result:
        result = result.strip().lower()
        for pt in PageType:
            if pt.value == result:
                return pt
    return PageType.UNKNOWN
