"""
Valyu Price Discovery Probe
---------------------------
Searches Valyu's API across web and proprietary sources,
prints results with per-article cost and content preview.

Usage:
    pip install valyu
    python valyu_probe.py --query "artificial intelligence"
    # Reads DSAIL_VALYU_API_KEY (fallback: VALYU_API_KEY), or pass --key
"""

import argparse
import json
import os
import sys

try:
    from valyu import Valyu
except ImportError:
    print("Run: pip install valyu")
    sys.exit(1)


def run(query: str, key: str, search_type: str = "all", max_results: int = 10):
    client = Valyu(key)

    print(f"\nQuery: '{query}'")
    print(f"Search type: {search_type}")
    print(f"Searching Valyu...\n")

    response = client.search(
        query,
        search_type=search_type,
        max_num_results=max_results,
        relevance_threshold=0.5,
    )

    if not response.success:
        print(f"Error: {response.error}")
        return []

    print(f"Found {len(response.results)} results")
    print(f"Total cost: ${response.total_deduction_dollars:.4f}")
    print(f"Sources — web: {response.results_by_source.web}, proprietary: {response.results_by_source.proprietary}")

    print("\n" + "=" * 72)
    print(f"VALYU SEARCH RESULTS — '{query}'")
    print("=" * 72)

    for i, r in enumerate(response.results, 1):
        print(f"\n[{i}] {r.title}")
        print(f"  URL:        {r.url}")
        print(f"  Source:     {r.source}  ({r.source_type})")
        print(f"  Published:  {r.publication_date or 'n/a'}")
        print(f"  Relevance:  {r.relevance_score:.2f}")
        print(f"  Price:      ${r.price:.4f}" if r.price else "  Price:      n/a")
        if r.content:
            preview = r.content[:200].replace("\n", " ")
            print(f"  Preview:    {preview}...")

    print("\n" + "=" * 72)
    return response.results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Valyu price discovery probe")
    parser.add_argument("--key",         required=False,          help="Valyu API key (defaults to DSAIL_VALYU_API_KEY or VALYU_API_KEY)")
    parser.add_argument("--query",       default="artificial intelligence", help="Search query")
    parser.add_argument("--type",        default="all",           help="all | web | proprietary | news")
    parser.add_argument("--max",         default=10, type=int,    help="Max results")
    parser.add_argument("--output",      default="valyu_results.json", help="JSON output file")
    args = parser.parse_args()

    key = (args.key or os.environ.get("DSAIL_VALYU_API_KEY") or os.environ.get("VALYU_API_KEY") or "").strip()
    if not key:
        parser.error("Missing Valyu API key. Set DSAIL_VALYU_API_KEY (or VALYU_API_KEY), or pass --key.")

    results = run(args.query, key, search_type=args.type, max_results=args.max)

    out = [
        {
            "title":            r.title,
            "url":              r.url,
            "source":           r.source,
            "source_type":      r.source_type,
            "publication_date": r.publication_date,
            "relevance_score":  r.relevance_score,
            "price":            r.price,
            "content_preview":  (r.content or "")[:300],
        }
        for r in results
    ]
    with open(args.output, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nJSON saved to {args.output}")
