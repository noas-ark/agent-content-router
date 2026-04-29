"""
TollBit Price Discovery
-----------------------
1. Searches TollBit's Licensed Search API for articles across
   all enrolled publishers (7,000+)
2. Filters for articles flagged readyToLicense
3. Calls the Rates batch API to get prices — no payment made

TollBit has two standard licenses:
  SUMMARIZATION  — use content once for RAG, citation, or grounding.
                   This is what an AI agent needs for answering questions.
  FULL DISPLAY   — display the full article in your application once.
                   Neither license permits AI training.

Usage:
    pip install requests
    python tollbit_probe.py --key YOUR_TOLLBIT_KEY --query "AI agents"

Get a free TollBit developer key at:
    https://app.tollbit.com  (Developer section)
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Optional

try:
    import requests
except ImportError:
    print("Run: pip install requests")
    sys.exit(1)

BASE = "https://gateway.tollbit.com"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    title: str
    url: str
    publisher_name: str
    publisher_domain: str
    published_date: str
    discoverable: bool
    ready_to_license: bool


@dataclass
class PricedArticle:
    title: str
    url: str
    publisher: str
    published_date: str
    # Summarization license: use once for RAG / citation / grounding
    summarization_usd: Optional[float]
    # Full Display license: display full article in your app once
    full_display_usd: Optional[float]
    raw_rates: list = field(default_factory=list)


# ── API helpers ───────────────────────────────────────────────────────────────

def search(query: str, key: str, size: int = 20) -> list[SearchResult]:
    """
    TollBit Licensed Search API.
    Searches across all 7,000+ enrolled publishers.
    Returns articles with availability flags.
    Under the hood: keyword search over TollBit's own index
    of crawled publisher content. Not semantic — closer to
    a site-restricted keyword search.
    """
    params = {"q": query, "size": size}
    resp = requests.get(
        f"{BASE}/dev/v2/search",
        headers={"TollbitKey": key},
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("items", []):
        pub = item.get("publisher", {})
        avail = item.get("availability", {})
        results.append(SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            publisher_name=pub.get("name", ""),
            publisher_domain=pub.get("domain", ""),
            published_date=item.get("publishedDate", ""),
            discoverable=avail.get("discoverable", False),
            ready_to_license=avail.get("readyToLicense", False),
        ))
    return results


def get_rates_batch(urls: list[str], key: str) -> dict[str, list]:
    """
    TollBit Rates batch API.
    Returns {url: [rate_options]} — no payment made.
    """
    resp = requests.post(
        f"{BASE}/tollbit/dev/v2/rate/batch",
        headers={"TollbitKey": key, "Content-Type": "application/json"},
        json={"urls": urls},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    result = {}
    for item in data:
        url = item.get("url", "")
        result[url] = item.get("rates", [])
    return result


def micros_to_usd(micros: int) -> float:
    return micros / 1_000_000


def parse_rates(rates: list) -> tuple[Optional[float], Optional[float]]:
    """
    Extract Summarization and Full Display prices from a rates list.

    TollBit license types:
      ON_DEMAND_LICENSE          → Summarization
        Use once for RAG, citation, or grounding an AI answer.
        Cannot train on it or display the full article.

      ON_DEMAND_FULL_USE_LICENSE → Full Display
        Display the full article in your application once.
        Cannot use for AI training.
    """
    summarization = None
    full_display = None
    for r in rates:
        license_type = r.get("license", {}).get("licenseType", "")
        price_micros = r.get("price", {}).get("priceMicros", 0)
        usd = micros_to_usd(price_micros)
        if license_type == "ON_DEMAND_LICENSE":
            summarization = usd
        elif license_type == "ON_DEMAND_FULL_USE_LICENSE":
            full_display = usd
    return summarization, full_display


# ── Main workflow ─────────────────────────────────────────────────────────────

def run(query: str, key: str) -> list[PricedArticle]:

    print(f"\nQuery: '{query}'")
    print(f"Searching across all TollBit publishers...")

    results = search(query, key, size=20)
    print(f"  {len(results)} articles found")

    licensable = [r for r in results if r.ready_to_license]
    print(f"  {len(licensable)} flagged readyToLicense")

    if not licensable:
        print("\nNo licensable articles found. Try a broader query.")
        return []

    print(f"\nProbing prices (no payment)...")
    urls = [r.url for r in licensable]
    rates_by_url = get_rates_batch(urls, key)

    priced = []
    for result in licensable:
        rates = rates_by_url.get(result.url) or []
        summarization, full_display = parse_rates(rates)
        priced.append(PricedArticle(
            title=result.title,
            url=result.url,
            publisher=result.publisher_name or result.publisher_domain,
            published_date=result.published_date,
            summarization_usd=summarization,
            full_display_usd=full_display,
            raw_rates=rates,
        ))

    return priced


def print_report(articles: list[PricedArticle], query: str):
    print("\n" + "=" * 72)
    print(f"TOLLBIT PRICE DISCOVERY — '{query}'")
    print("=" * 72)
    print("SUMMARIZATION  use once for RAG / citation / grounding an AI answer")
    print("FULL DISPLAY   display full article in your app once")
    print("Neither license permits AI training.")
    print("=" * 72)

    if not articles:
        print("No priced results.")
        return

    articles.sort(key=lambda a: a.summarization_usd or 999)

    for a in articles:
        summ = f"${a.summarization_usd:.4f}" if a.summarization_usd is not None else "n/a"
        full = f"${a.full_display_usd:.4f}"  if a.full_display_usd  is not None else "n/a"
        print(f"\n  Title:         {a.title}")
        print(f"  URL:           {a.url}")
        print(f"  Publisher:     {a.publisher}")
        print(f"  Published:     {a.published_date}")
        print(f"  Summarization: {summ}")
        print(f"  Full Display:  {full}")

    priced = [a for a in articles if a.summarization_usd is not None]
    if priced:
        prices = [a.summarization_usd for a in priced]
        print(f"\nSummarization price range: ${min(prices):.4f} – ${max(prices):.4f}")
        print(f"Avg: ${sum(prices)/len(prices):.4f} across {len(priced)} articles")

    print("=" * 72)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TollBit price discovery")
    parser.add_argument("--key",    required=True, help="TollBit API key")
    parser.add_argument("--query",  default="artificial intelligence",
                                    help="Search query (natural language)")
    parser.add_argument("--output", default="tollbit_prices.json",
                                    help="JSON output file")
    args = parser.parse_args()

    try:
        articles = run(args.query, args.key)
        print_report(articles, args.query)

        out = [
            {
                "title":             a.title,
                "url":               a.url,
                "publisher":         a.publisher,
                "published_date":    a.published_date,
                "summarization_usd": a.summarization_usd,
                "full_display_usd":  a.full_display_usd,
                "notes": {
                    "summarization": "Use once for RAG, citation, or grounding. Cannot train or display full article.",
                    "full_display":  "Display full article in app once. Cannot train."
                }
            }
            for a in articles
        ]
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nJSON saved to {args.output}")

    except requests.exceptions.HTTPError as e:
        print(f"\nAPI error: {e}")
        print("Check your TollBit key at app.tollbit.com → Developer section.")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)
