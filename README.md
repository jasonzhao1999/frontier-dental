# Safco Dental Product Scraper

Scrapes product data from [Safco Dental Supply](https://www.safcodental.com) using a multi-agent pipeline. This is a POC — it works end to end on the two target categories but would need some more work before running against the full site.

## How it works

```
                          +----------------+
                          |  Orchestrator  |
                          |   (run.py)     |
                          +-------+--------+
                                  |
          +-----------------------+-----------------------+
          |                       |                       |
    +-----v-----+          +-----v-----+          +------v-----+
    | Navigator |          | Extractor |          | Validator  |
    +-----+-----+          +-----+-----+          +------+-----+
          |                      |                       |
          |                      |                       |
    +-----v----------------------v-----------------------v-----+
    |                     Core Layer                            |
    |  Fetcher (rate-limited HTTP) | Storage (SQLite + export)  |
    |  Classifier (page typing)    | LLM client (optional)      |
    +--------------------------------------------------------------+
```

The scraping problem breaks down pretty naturally into a few separate jobs, so I split them up:

- **Navigator** — figures out what URLs to visit. It starts from the seed categories, finds subcategories by parsing links and JSON-LD data, and hands URLs off to the rest of the pipeline. If I need to add sitemap parsing later, I only touch this file.

- **Classifier** — looks at a page and decides what type it is (category, listing, product page, etc). It checks URL patterns first, then HTML structure, and only calls the LLM if it still can't tell. Most pages get classified without any API calls.

- **Extractor** — pulls product data out of pages. It tries JSON-LD first since that's structured and reliable, falls back to scraping HTML tags, and uses the LLM as a last resort. This way we're not burning API credits on pages where we don't need to.

- **Validator** — sits between extraction and storage. Drops anything missing a name or URL, normalizes text, flags weird prices, and deduplicates using a hash of URL + SKU.

The orchestrator in `run.py` just loops through everything sequentially. For a POC that's fine — it's easy to follow and debug. In production I'd swap it for something like Celery so categories can be processed in parallel.

| Agent | What it does | Takes in | Gives back |
|-------|-------------|----------|------------|
| Navigator | Finds subcategories and product URLs | Seed URLs | `Category` objects, product URLs |
| Classifier | Identifies page type | HTML + URL | `PageType` enum |
| Extractor | Pulls product fields from pages | HTML | `ProductRecord` list |
| Validator | Cleans and deduplicates | Raw records | Clean records |

## Setup

You need Python 3.10+ and optionally an Anthropic API key (for the LLM-based classification/extraction fallbacks — the scraper still works without one, it just skips those steps).

```bash
git clone <repo-url>
cd frontier_dental
python -m venv .venv
source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements.txt

# If you want LLM features
cp .env.example .env
# Add your ANTHROPIC_API_KEY to .env
```

## Running

```bash
python run.py                          # scrape both categories
python run.py --skip-details           # faster — skip individual product pages
python run.py --categories 1           # only scrape the first category
python run.py --export-only            # just re-export existing data to JSON/CSV
python run.py --config other.yaml      # use a different config
```

Output goes to `output/`:
- `products.json` — structured export
- `products.csv` — flat export (opens in Excel)
- `products.db` — SQLite database you can query directly
- `scraper.log` — log file

## Configuration

Everything is in `config.yaml`:

```yaml
target:
  base_url: "https://www.safcodental.com"
  seed_categories:
    - "/catalog/sutures-surgical-products"
    - "/catalog/gloves"

scraping:
  request_delay: 1.5    # seconds between requests
  max_retries: 3
  timeout: 30
  max_concurrency: 3

llm:
  model: "claude-sonnet-4-20250514"
  max_tokens: 1024
```

To scrape a new category, just add it to `seed_categories`.

## Output schema

| Field | Type | Example |
|-------|------|---------|
| `product_name` | string | "Safco SureStitch sutures" |
| `brand` | string / null | "Safco Dental" |
| `sku` | string / null | "PFEMN" |
| `category_path` | string | "sutures-surgical-products > Sutures" |
| `product_url` | string | "https://www.safcodental.com/product/..." |
| `price` | float / null | 30.49 |
| `currency` | string | "USD" |
| `unit_pack_size` | string / null | "Box of 200" |
| `availability` | string / null | "In Stock" |
| `description` | string / null | Product description text |
| `specifications` | object | `{"material": "nitrile"}` |
| `image_urls` | array | `["https://...jpg"]` |
| `alternative_products` | array | `["https://..."]` |
| `rating` | float / null | 4.5 |
| `review_count` | int / null | 12 |
| `fingerprint` | string | MD5 hash for dedup |
| `scraped_at` | string | ISO 8601 timestamp |

## Sample output

Last run pulled 141 products across both categories:

```
Sutures & Surgical Products: 56 products (8 subcategories)
Dental Exam Gloves: 85 products (6 subcategories)

Field coverage:
  Price:       141/141 (100%)
  Images:      141/141 (100%)
  SKU:         141/141 (100%)
  Rating:      89/141  (63%)
  Description: 6/141   (4%) -- detail pages are JS-rendered, see limitations
```

## Failure handling

**HTTP failures** — the fetcher retries server errors (5xx) and transport errors up to 3 times with exponential backoff. 4xx errors aren't retried since they usually mean the page doesn't exist. There's a 1.5s delay between requests to avoid getting blocked.

**Extraction failures** — if JSON-LD parsing fails, it tries HTML selectors, then the LLM. If all three fail it logs it and moves on. One broken page doesn't kill the whole run.

**Crashes / interrupted runs** — visited URLs are tracked in SQLite, so if the scraper stops mid-run you can just run it again and it picks up where it left off. The UNIQUE constraint on the product fingerprint prevents duplicates.

**Bad data** — the validator drops records missing required fields, flags prices that look wrong (negative or over $50k), and normalizes whitespace/encoding issues.

## Limitations

1. **Detail pages are JS-rendered.** Safco runs Magento with the Hyva theme (Alpine.js), so product detail pages load their content with JavaScript. Since I'm using `httpx` (not a browser), I can't get descriptions, specs, or pack sizes from those pages. The listing pages have good JSON-LD data though, which is where most of the product info comes from. Using Playwright for detail pages would fix this.

2. **Pagination isn't fully tested.** The navigator looks for pagination links, but none of the subcategories in the test set were paginated, so that code path hasn't been exercised. Nitrile gloves shows 50 products which might not be the full count.

3. **No proxy rotation.** Fine for two categories, but full-site scraping would probably need rotating proxies.

4. **LLM extraction isn't deterministic.** The fallback works but you might get slightly different results each time. In production I'd want confidence scores and human review for LLM-extracted data.

5. **No variant grouping.** Products with size/color variants are treated as separate entries.

## Scaling to production

What I'd change to run this for real:

**Infrastructure** — replace the sequential loop with a task queue (Celery + Redis) so categories process in parallel. Add a Playwright pool for JS-rendered pages. Set up proxy rotation.

**Reliability** — move checkpointing to Redis or Postgres with TTL and locking. Add a circuit breaker so if a subcategory keeps failing we back off instead of retrying forever. Failed pages should go to a dead letter queue for manual review.

**Monitoring** — track field coverage after each run (if price drops below 90% populated, something probably broke). Compare runs against each other to catch big swings (products disappearing, price changes >50%). Check the site's JSON-LD structure against a known-good snapshot to catch schema changes early.

**Deployment** — Dockerize it, run on a schedule (cron or Airflow), move API keys to a proper secret store (AWS Secrets Manager or similar), and ship logs somewhere centralized like Datadog.

## Project structure

```
fronter_dental/
├── run.py                 # entry point + orchestrator
├── config.yaml            # settings
├── requirements.txt
├── .env.example
├── .gitignore
├── scraper/
│   ├── __init__.py
│   ├── config.py          # config loading
│   ├── fetcher.py         # async HTTP with rate limiting + retries
│   ├── models.py          # pydantic data models
│   ├── storage.py         # SQLite + CSV/JSON export
│   ├── llm.py             # Anthropic API wrapper
│   ├── navigator.py       # URL discovery
│   ├── classifier.py      # page type detection
│   ├── extractor.py       # product data extraction
│   └── validator.py       # cleaning + dedup
└── output/
    ├── products.json
    ├── products.csv
    ├── products.db
    └── scraper.log
```
