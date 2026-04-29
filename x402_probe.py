"""
x402 Price Discovery Probe
--------------------------
Probes x402-enabled endpoints and returns their prices
WITHOUT making any payment or requiring a wallet.

The 402 response itself contains machine-readable payment requirements.
We just read and decode them.

Usage:
    pip install requests
    python x402_probe.py

Or probe a specific URL:
    python x402_probe.py https://example.com/paid-content
"""

import base64
import json
import sys
import time
from dataclasses import dataclass
from typing import Optional

try:
    import requests
except ImportError:
    print("Run: pip install requests")
    sys.exit(1)


# ── Known x402 endpoints from x402scan.com / public registrations ────────────
# These are real endpoints observed in the x402 ecosystem.
# Sourced from: x402scan.com, coinbase/x402 examples repo, dev.to posts.
# Format: (url, description, expected_domain)

DEFAULT_ENDPOINTS = [
    # Coinbase reference implementation (testnet demo)
    ("https://x402.org/demo/protected",             "x402 reference demo endpoint"),
    # Crypto enrichment API (Railway deployment, live)
    ("https://crypto-enrichment-api-production.up.railway.app/api/v1/price/BTC",
                                                     "BTC price - crypto enrichment API"),
    ("https://crypto-enrichment-api-production.up.railway.app/api/v1/analysis/ETH",
                                                     "ETH analysis - crypto enrichment API"),
    # enrichx402.com - data enrichment marketplace
    ("https://enrichx402.com/api/apollo/people-enrich",  "Apollo people enrichment"),
    ("https://enrichx402.com/api/hunter/domain-search",  "Hunter.io domain search"),
    # Messari research (x402-gated)
    ("https://api.messari.io/x402/asset/bitcoin/metrics",  "Messari BTC metrics"),
    # Nansen analytics
    ("https://api.nansen.ai/x402/wallet/label",      "Nansen wallet label"),
    # Generic x402 test server (Coinbase examples)
    ("https://x402-example.vercel.app/api/protected", "Vercel example - protected"),
    ("https://x402-example.vercel.app/api/weather",   "Vercel example - weather"),
]


@dataclass
class PriceQuote:
    url: str
    description: str
    status: int                    # 402 = paywall detected, other = unexpected
    price_usdc: Optional[float]    # price in USDC (human-readable)
    price_raw: Optional[str]       # raw maxAmountRequired (token base units)
    network: Optional[str]         # e.g. "base", "base-sepolia", "solana"
    asset: Optional[str]           # token contract address
    scheme: Optional[str]          # e.g. "exact"
    content_description: Optional[str]
    error: Optional[str]
    latency_ms: int


def decode_payment_required(header_value: str) -> Optional[dict]:
    """
    Decode the PAYMENT-REQUIRED header.
    x402 v1: base64-encoded JSON with structure:
      { x402Version, accepts: [{ scheme, network, maxAmountRequired,
                                  payTo, asset, resource, description }] }
    x402 v2: similar but payment data moved entirely to headers.
    """
    try:
        # Strip any padding issues
        padded = header_value + "=" * (4 - len(header_value) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        # Some implementations return raw JSON instead of base64
        try:
            return json.loads(header_value)
        except Exception:
            return None


def usdc_from_raw(raw: str, decimals: int = 6) -> Optional[float]:
    """Convert token base units to human-readable USDC (6 decimals)."""
    try:
        return int(raw) / (10 ** decimals)
    except Exception:
        return None


def probe(url: str, description: str = "", timeout: int = 8) -> PriceQuote:
    """
    Make an unauthenticated GET request to a URL.
    If it returns 402, decode the payment requirements.
    No payment is made. No wallet needed.
    """
    t0 = time.time()
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "bootk.ai-price-probe/0.1 (demand-side intelligence)",
                "Accept": "application/json, text/html, */*",
            },
            allow_redirects=False,
        )
        latency_ms = int((time.time() - t0) * 1000)

        if resp.status_code != 402:
            return PriceQuote(
                url=url, description=description, status=resp.status_code,
                price_usdc=None, price_raw=None, network=None, asset=None,
                scheme=None, content_description=None,
                error=f"Expected 402, got {resp.status_code}",
                latency_ms=latency_ms,
            )

        # Try PAYMENT-REQUIRED header first (x402 v1/v2 spec)
        payment_header = (
            resp.headers.get("PAYMENT-REQUIRED") or
            resp.headers.get("X-Payment-Required") or
            resp.headers.get("payment-required")
        )

        payload = None
        if payment_header:
            payload = decode_payment_required(payment_header)

        # Fallback: try response body (some implementations put JSON there)
        if not payload:
            try:
                payload = resp.json()
            except Exception:
                pass

        if not payload:
            return PriceQuote(
                url=url, description=description, status=402,
                price_usdc=None, price_raw=None, network=None, asset=None,
                scheme=None, content_description=None,
                error="402 received but could not decode payment requirements",
                latency_ms=latency_ms,
            )

        # Parse the accepts array (x402 structure)
        accepts = payload.get("accepts", [])
        if not accepts and "maxAmountRequired" in payload:
            # Flat structure (some implementations)
            accepts = [payload]

        if not accepts:
            return PriceQuote(
                url=url, description=description, status=402,
                price_usdc=None, price_raw=None, network=None, asset=None,
                scheme=None, content_description=None,
                error="No payment options in 402 response",
                latency_ms=latency_ms,
            )

        # Take first payment option (prefer base mainnet if multiple)
        option = next(
            (a for a in accepts if "base" in a.get("network", "") and "sepolia" not in a.get("network", "")),
            accepts[0]
        )

        raw = option.get("maxAmountRequired") or option.get("amount") or option.get("price")
        price_usdc = usdc_from_raw(str(raw)) if raw else None
        content_desc = (
            option.get("description") or
            payload.get("description") or
            description
        )

        return PriceQuote(
            url=url,
            description=description,
            status=402,
            price_usdc=price_usdc,
            price_raw=str(raw) if raw else None,
            network=option.get("network"),
            asset=option.get("asset"),
            scheme=option.get("scheme"),
            content_description=content_desc,
            error=None,
            latency_ms=latency_ms,
        )

    except requests.exceptions.Timeout:
        return PriceQuote(
            url=url, description=description, status=0,
            price_usdc=None, price_raw=None, network=None, asset=None,
            scheme=None, content_description=None,
            error=f"Timeout after {timeout}s",
            latency_ms=int((time.time() - t0) * 1000),
        )
    except Exception as e:
        return PriceQuote(
            url=url, description=description, status=0,
            price_usdc=None, price_raw=None, network=None, asset=None,
            scheme=None, content_description=None,
            error=str(e),
            latency_ms=int((time.time() - t0) * 1000),
        )


def probe_batch(endpoints: list[tuple]) -> list[PriceQuote]:
    """Probe a list of (url, description) tuples and return price quotes."""
    results = []
    for url, desc in endpoints:
        print(f"  probing {url[:60]}...", end=" ", flush=True)
        q = probe(url, desc)
        if q.status == 402 and q.price_usdc is not None:
            print(f"${q.price_usdc:.4f} USDC [{q.network}]")
        elif q.status == 402:
            print(f"402 (price unparseable)")
        else:
            print(f"{q.error or q.status}")
        results.append(q)
    return results


def print_report(quotes: list[PriceQuote]):
    """Print a structured price discovery report."""
    paywall_hits = [q for q in quotes if q.status == 402 and q.price_usdc is not None]
    errors       = [q for q in quotes if q.error]

    print("\n" + "=" * 60)
    print("x402 PRICE DISCOVERY REPORT")
    print("=" * 60)

    if paywall_hits:
        print(f"\n{'ENDPOINT':<45} {'PRICE (USDC)':>12}  {'NETWORK'}")
        print("-" * 75)
        for q in sorted(paywall_hits, key=lambda x: x.price_usdc or 0):
            endpoint = q.url[:44]
            price    = f"${q.price_usdc:.4f}"
            network  = q.network or "unknown"
            print(f"{endpoint:<45} {price:>12}  {network}")

    if errors:
        print(f"\nUnreachable / unexpected ({len(errors)}):")
        for q in errors:
            print(f"  {q.url[:55]}  →  {q.error}")

    if paywall_hits:
        prices = [q.price_usdc for q in paywall_hits]
        print(f"\nSummary: {len(paywall_hits)} priced endpoints")
        print(f"  min: ${min(prices):.4f}")
        print(f"  max: ${max(prices):.4f}")
        print(f"  avg: ${sum(prices)/len(prices):.4f}")

    print("=" * 60)


def to_json(quotes: list[PriceQuote]) -> str:
    """Export quotes as JSON for downstream use (e.g. triage scorer input)."""
    return json.dumps(
        [
            {
                "url":         q.url,
                "description": q.content_description or q.description,
                "price_usdc":  q.price_usdc,
                "network":     q.network,
                "latency_ms":  q.latency_ms,
                "status":      q.status,
                "error":       q.error,
            }
            for q in quotes
        ],
        indent=2,
    )


if __name__ == "__main__":
    # If URL passed as arg, probe just that one
    if len(sys.argv) > 1:
        url = sys.argv[1]
        print(f"\nProbing: {url}")
        q = probe(url, "manual probe")
        print(json.dumps({
            "status":      q.status,
            "price_usdc":  q.price_usdc,
            "network":     q.network,
            "description": q.content_description,
            "raw_payload": q.price_raw,
            "error":       q.error,
        }, indent=2))
        sys.exit(0)

    print("x402 Price Discovery Probe")
    print(f"Probing {len(DEFAULT_ENDPOINTS)} known endpoints...\n")

    quotes = probe_batch(DEFAULT_ENDPOINTS)
    print_report(quotes)

    # Dump JSON for piping into triage scorer
    with open("x402_prices.json", "w") as f:
        f.write(to_json(quotes))
    print(f"\nJSON output saved to x402_prices.json")
