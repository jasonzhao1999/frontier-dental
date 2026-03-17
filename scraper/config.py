"""
Loads config.yaml + .env into typed objects.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

_ROOT = Path(__file__).resolve().parent.parent


class TargetConfig(BaseModel):
    base_url: str
    seed_categories: list[str]


class ScrapingConfig(BaseModel):
    request_delay: float = 1.5
    max_retries: int = 3
    timeout: int = 30
    max_concurrency: int = 3
    user_agent: str = "Mozilla/5.0"


class LLMConfig(BaseModel):
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 1024


class StorageConfig(BaseModel):
    db_path: str = "output/products.db"
    export_json: str = "output/products.json"
    export_csv: str = "output/products.csv"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "output/scraper.log"


class AppConfig(BaseModel):
    target: TargetConfig
    scraping: ScrapingConfig = ScrapingConfig()
    llm: LLMConfig = LLMConfig()
    storage: StorageConfig = StorageConfig()
    logging: LoggingConfig = LoggingConfig()


def load_config(path: Optional[str] = None) -> AppConfig:
    config_path = Path(path) if path else _ROOT / "config.yaml"
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    return AppConfig(**raw)


def get_api_key() -> Optional[str]:
    return os.getenv("ANTHROPIC_API_KEY")
