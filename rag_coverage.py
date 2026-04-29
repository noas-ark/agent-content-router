"""
RAG-style coverage for free search hits: fetch public HTML, chunk, embed with
OpenAI text-embedding-3-small (API call, zero local memory), max cosine vs
sub-query embedding. Falls back to snippet_score when fetch fails, paywall
skip, or content too short.

Requires OPENAI_API_KEY env var. If unset, RAG is disabled and snippet scores
are used directly — same behaviour as before but without the PyTorch footprint.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

import requests

# Known soft-paywall / JS-heavy news domains — skip full fetch; snippet-only scoring.
PAYWALL_HOST_SUFFIXES: Tuple[str, ...] = (
    "nytimes.com",
    "ft.com",
    "wsj.com",
    "washingtonpost.com",
    "economist.com",
    "barrons.com",
    "theatlantic.com",
    "thetimes.co.uk",
)

MIN_WORDS_FOR_RAG = 200
MAX_CHUNK_WORDS = 400
CHUNK_STRIDE = 200
MAX_CHUNKS_PER_URL = 10
FETCH_TIMEOUT = 8.0
MAX_BYTES = 2_000_000

OPENAI_EMBED_MODEL = "text-embedding-3-small"
OPENAI_EMBED_BATCH = 64  # max texts per API call

_CLIENT: Any = None
_CLIENT_FAILED = False


def _get_model():
    """Return an OpenAI client if OPENAI_API_KEY is set, else None.

    Named _get_model() for compatibility with app.py callers that check
    `_get_model() is not None` to decide whether RAG is active.
    """
    global _CLIENT, _CLIENT_FAILED
    if _CLIENT_FAILED:
        return None
    if _CLIENT is not None:
        return _CLIENT
    if os.environ.get("BOOTK_DISABLE_RAG", "").strip().lower() in ("1", "true", "yes"):
        return None
    api_key = (os.environ.get("OPENAI_API_KEY") or os.environ.get("DSAIL_OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None
    try:
        from openai import OpenAI
        _CLIENT = OpenAI(api_key=api_key)
        return _CLIENT
    except Exception:
        _CLIENT_FAILED = True
        return None


def warm_embedding_model() -> None:
    """No-op for API-based embeddings — no local model to pre-load."""
    client = _get_model()
    if client is not None:
        import logging
        logging.getLogger(__name__).info(
            "RAG embeddings: OpenAI %s ready", OPENAI_EMBED_MODEL
        )
    else:
        import logging
        logging.getLogger(__name__).info(
            "RAG embeddings: disabled (OPENAI_API_KEY not set or BOOTK_DISABLE_RAG=1)"
        )


def host_paywalled(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        return True
    for suf in PAYWALL_HOST_SUFFIXES:
        if host == suf or host.endswith("." + suf):
            return True
    return False


def _html_to_text(raw: bytes, content_type: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return ""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype and ctype not in (
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "application/octet-stream",
    ):
        # e.g. application/pdf — skip
        return ""
    text = raw.decode("utf-8", errors="replace")
    if not ctype or "html" in ctype or text.strip().startswith("<"):
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    return text


def fetch_page(url: str) -> Dict[str, Any]:
    try:
        r = requests.get(
            url,
            timeout=FETCH_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; bootk.ai/1.0; content-routing evaluation)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            },
            allow_redirects=True,
        )
        r.raise_for_status()
        if len(r.content) > MAX_BYTES:
            return {"ok": False, "text": "", "n_words": 0, "content_type": "", "reason": "too_large"}
        ct = r.headers.get("Content-Type") or ""
        text = _html_to_text(r.content, ct)
        words = text.split()
        n = len(words)
        return {
            "ok": True,
            "text": " ".join(words),
            "n_words": n,
            "content_type": ct,
            "reason": "ok",
        }
    except Exception as e:
        return {"ok": False, "text": "", "n_words": 0, "content_type": "", "reason": str(e)[:120]}


def chunk_text(text: str) -> List[str]:
    words = text.split()
    if not words:
        return []
    chunks: List[str] = []
    i = 0
    while i < len(words) and len(chunks) < MAX_CHUNKS_PER_URL:
        chunk = words[i : i + MAX_CHUNK_WORDS]
        if len(chunk) < 50:
            break
        chunks.append(" ".join(chunk))
        i += CHUNK_STRIDE
    if not chunks and words:
        chunks.append(" ".join(words[: min(len(words), MAX_CHUNK_WORDS)]))
    return chunks


def cosine_to_unit(x: float) -> float:
    """Map cosine from [-1, 1] to [0, 1] for coverage."""
    return max(0.0, min(1.0, (float(x) + 1.0) / 2.0))


def effective_coverage(r: Dict[str, Any]) -> float:
    ss = float(r.get("snippet_score") or 0)
    rs = r.get("rag_score")
    if rs is None:
        return ss
    return max(float(rs), ss)


def _openai_embed(client: Any, texts: List[str]) -> List[List[float]]:
    """Embed a list of texts via OpenAI API, batching if needed."""
    all_embeddings: List[List[float]] = []
    for i in range(0, len(texts), OPENAI_EMBED_BATCH):
        batch = texts[i : i + OPENAI_EMBED_BATCH]
        resp = client.embeddings.create(input=batch, model=OPENAI_EMBED_MODEL)
        # resp.data is sorted by index
        all_embeddings.extend([item.embedding for item in resp.data])
    return all_embeddings


def _cosine(a: List[float], b: List[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def enrich_free_results_with_rag(
    results: List[Dict[str, Any]],
    sub_query: str,
    deadline: float,
) -> None:
    """
    Mutates each result dict: expects snippet_score set by caller; adds rag_score,
    rag_status, coverage_score = max(rag_score, snippet_score) when applicable.
    """
    if os.environ.get("BOOTK_DISABLE_RAG", "").strip().lower() in ("1", "true", "yes"):
        for r in results:
            r["coverage_score"] = effective_coverage(r)
        return

    client = _get_model()
    if client is None:
        for r in results:
            r["coverage_score"] = effective_coverage(r)
        return

    fetch_jobs: List[Tuple[int, str]] = []
    for i, r in enumerate(results):
        url = (r.get("url") or "").strip()
        r.setdefault("rag_score", None)
        r.setdefault("rag_status", None)
        if not url:
            r["rag_status"] = "no_url"
            continue
        if host_paywalled(url):
            r["rag_status"] = "skip_paywall"
            continue
        fetch_jobs.append((i, url))
        r["rag_status"] = "pending"

    fetched: Dict[int, Dict[str, Any]] = {}
    if fetch_jobs:
        with ThreadPoolExecutor(max_workers=min(5, len(fetch_jobs))) as ex:
            fut_map = {ex.submit(fetch_page, url): idx for idx, url in fetch_jobs}
            for fut in as_completed(fut_map):
                idx = fut_map[fut]
                if time.time() > deadline:
                    break
                try:
                    fetched[idx] = fut.result()
                except Exception as e:
                    fetched[idx] = {"ok": False, "reason": str(e)[:120]}

    chunk_meta: List[int] = []
    flat_chunks: List[str] = []

    for i, r in enumerate(results):
        if r.get("rag_status") == "skip_paywall":
            continue
        if i not in fetched:
            if r.get("rag_status") == "pending":
                r["rag_status"] = "not_fetched"
            continue
        fr = fetched[i]
        r["rag_words_fetched"] = fr.get("n_words", 0)
        if not fr.get("ok"):
            r["rag_status"] = f"fetch_failed:{fr.get('reason', 'unknown')}"
            r["rag_fetch_error"] = fr.get("reason", "unknown")
            continue
        if fr.get("n_words", 0) < MIN_WORDS_FOR_RAG:
            r["rag_status"] = "fetch_short"
            continue
        chs = chunk_text(fr.get("text") or "")
        r["rag_chunks_count"] = len(chs)
        if not chs:
            r["rag_status"] = "no_chunks"
            continue
        for ch in chs:
            chunk_meta.append(i)
            flat_chunks.append(ch)

    if time.time() > deadline or not flat_chunks:
        for r in results:
            r["coverage_score"] = effective_coverage(r)
        return

    try:
        all_texts = [sub_query] + flat_chunks
        all_embeddings = _openai_embed(client, all_texts)
        emb_q = all_embeddings[0]
        emb_chunks = all_embeddings[1:]

        max_per: Dict[int, float] = defaultdict(float)
        for row, res_idx in enumerate(chunk_meta):
            sim = cosine_to_unit(_cosine(emb_chunks[row], emb_q))
            max_per[res_idx] = max(max_per[res_idx], sim)
        for i, r in enumerate(results):
            if i in max_per:
                r["rag_score"] = round(max_per[i], 4)
                r["rag_status"] = "ok"
    except Exception as e:
        for i in set(chunk_meta):
            if 0 <= i < len(results):
                results[i]["rag_score"] = None
                results[i]["rag_status"] = f"embed_failed:{str(e)[:80]}"

    for r in results:
        r["coverage_score"] = round(effective_coverage(r), 4)


def is_gap(results_with_scores: List[Dict[str, Any]], quality_threshold: float) -> bool:
    """True if no result clears the quality bar using RAG ∪ snippet scores."""
    if not results_with_scores:
        return True
    best = max(effective_coverage(r) for r in results_with_scores)
    return best < float(quality_threshold or 0.0)
