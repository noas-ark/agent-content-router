"""
Real-time search integration for the purchase optimizer.
Maps search result URLs to catalog sources so the purchase plan is backed by actual articles to scrape.

Provider order with fallback (first to return results wins):
  1) Brave Search — BRAVE_API_KEY
  2) Google Custom Search — GOOGLE_CSE_API_KEY + GOOGLE_CSE_CX
  3) DuckDuckGo — no key (always available)
"""

import os
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

try:
    import urllib.request
    import urllib.parse
    import json as _json
except ImportError:
    pass


def _normalize_domain(link: str) -> str:
    try:
        parsed = urlparse(link)
        host = (parsed.netloc or "").lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


# ─── Brave Search ─────────────────────────────────────────────────────────
def fetch_brave(query: str, api_key: str, num: int = 10) -> List[Dict[str, Any]]:
    """Call Brave Web Search API. Returns list of { title, link, snippet, displayLink }."""
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": min(num, 20)})
    req = urllib.request.Request(
        url,
        headers={
            "X-Subscription-Token": api_key,
            "Accept": "application/json",
            "User-Agent": "ContentPurchaseOptimizer/1.0",
        },
    )
    last_err = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = _json.loads(resp.read().decode())
            results = (data.get("web") or {}).get("results") or []
            return [
                {
                    "title": r.get("title", ""),
                    "link": r.get("url", ""),
                    "snippet": r.get("description", ""),
                    "displayLink": _normalize_domain(r.get("url", "")),
                }
                for r in results[:num]
            ]
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(1)
    return []


# ─── DuckDuckGo / metasearch (ddgs preferred; duckduckgo_search fallback) ───
def _normalize_ddgs_result(r: Dict[str, Any]) -> Dict[str, Any]:
    link = r.get("href") or r.get("link") or r.get("url") or ""
    return {
        "title": r.get("title", ""),
        "link": link,
        "snippet": r.get("body", r.get("snippet", "")),
        "displayLink": _normalize_domain(link),
    }


def fetch_duckduckgo(query: str, num: int = 10) -> List[Dict[str, Any]]:
    """Search via ddgs (metasearch) or legacy duckduckgo_search. Returns list of { title, link, snippet }."""
    num = min(num, 20)
    # Prefer modern ddgs package (Python 3.10+); avoids deprecation and often returns results
    try:
        from ddgs import DDGS
        last_err = None
        for attempt in range(2):
            try:
                client = DDGS()
                results = client.text(query, max_results=num, backend="auto")
                if results is None:
                    results = []
                results = list(results) if not isinstance(results, list) else results
                return [_normalize_ddgs_result(r) for r in results]
            except Exception as e:
                last_err = e
                if attempt == 0:
                    time.sleep(1)
        return []
    except ImportError:
        pass
    # Fallback: legacy duckduckgo_search (deprecated, may return 0 results)
    try:
        import warnings
        with warnings.catch_warnings(action="ignore", category=RuntimeWarning):
            from duckduckgo_search import DDGS
        for attempt in range(2):
            try:
                with DDGS() as ddgs:
                    raw = list(ddgs.text(query, max_results=num))
                return [_normalize_ddgs_result(r) for r in raw]
            except Exception:
                if attempt == 0:
                    time.sleep(1)
    except ImportError:
        pass
    return []


# ─── Google Custom Search ─────────────────────────────────────────────────
def fetch_google_cse(query: str, api_key: str, cx: str, num: int = 10) -> List[Dict[str, Any]]:
    """Call Google Custom Search JSON API. Returns list of { title, link, snippet, displayLink }."""
    base = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cx,
        "q": query,
        "num": min(num, 10),
    }
    url = base + "?" + "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    req = urllib.request.Request(url, headers={"User-Agent": "ContentPurchaseOptimizer/1.0"})
    last_err = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())
            items = data.get("items") or []
            return [
                {
                    "title": it.get("title", ""),
                    "link": it.get("link", ""),
                    "snippet": it.get("snippet", ""),
                    "displayLink": (it.get("displayLink") or _normalize_domain(it.get("link", ""))),
                }
                for it in items
            ]
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(1)
    return []


# ─── Site-restricted search (fallback to get articles from our catalog) ─────
def fetch_search_results_for_site(query: str, site_domain: str, num: int = 3) -> List[Dict[str, Any]]:
    """Search restricted to one domain (e.g. apnews.com). Use when general search returns no matches."""
    q = f"{query} site:{site_domain}"
    results, _ = fetch_search_results(q, num=num)
    return results


# ─── Unified entrypoint ────────────────────────────────────────────────────
def fetch_search_results(query: str, num: int = 12) -> Tuple[List[Dict[str, Any]], str]:
    """
    Fetch real-time search results for the query.
    Tries providers in order (Brave → Google CSE → DuckDuckGo); first to return
    non-empty results wins. Each provider is retried once on failure.
    Returns (list of { title, link, snippet, displayLink }, provider_name).
    """
    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if brave_key:
        results = fetch_brave(query, brave_key, num=num)
        if results:
            return (results, "Brave")
    api_key = os.environ.get("GOOGLE_CSE_API_KEY", "").strip()
    cx = os.environ.get("GOOGLE_CSE_CX", "").strip()
    if api_key and cx:
        results = fetch_google_cse(query, api_key, cx, num=num)
        if results:
            return (results, "Google CSE")
    results = fetch_duckduckgo(query, num=num)
    return (results, "DuckDuckGo")


def is_search_configured() -> bool:
    """True if search is available (DuckDuckGo is always available; others take precedence)."""
    return True


def get_search_provider_name() -> str:
    """Which provider will be used (for UI or logs)."""
    if os.environ.get("BRAVE_API_KEY", "").strip():
        return "Brave"
    if os.environ.get("GOOGLE_CSE_API_KEY", "").strip() and os.environ.get("GOOGLE_CSE_CX", "").strip():
        return "Google CSE"
    return "DuckDuckGo"
