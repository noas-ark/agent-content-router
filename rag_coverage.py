"""
RAG-style coverage for free search hits: fetch public HTML, chunk, embed with
all-MiniLM-L6-v2, max cosine vs sub-query embedding. Falls back to snippet_score
when fetch fails, paywall skip, or content too short.
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

_MODEL: Any = None
_MODEL_FAILED = False


def _rag_disabled() -> bool:
    if os.environ.get("BOOTK_DISABLE_RAG", "").strip().lower() in ("1", "true", "yes"):
        return True
    on_render = bool(os.environ.get("RENDER", "").strip())
    force_enable = os.environ.get("BOOTK_ENABLE_RAG", "").strip().lower() in ("1", "true", "yes")
    return on_render and not force_enable


def _get_model():
    global _MODEL, _MODEL_FAILED
    if _MODEL_FAILED or _rag_disabled():
        return None
    if _MODEL is not None:
        return _MODEL
    try:
        from sentence_transformers import SentenceTransformer

        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        return _MODEL
    except Exception:
        _MODEL_FAILED = True
        return None


def warm_embedding_model() -> None:
    """Load the sentence-transformers model in the background so the first /optimize isn’t blocked.

    Auto-disabled on Render free/starter tier (<=512 MB) because PyTorch alone uses ~250 MB.
    Set BOOTK_ENABLE_RAG=1 to force-enable on a larger Render instance (>=1 GB RAM).
    Set BOOTK_DISABLE_RAG=1 to force-disable anywhere.
    """
    disable = os.environ.get("BOOTK_DISABLE_RAG", "").strip().lower() in ("1", "true", "yes")
    if disable:
        return
    # On Render, skip unless the operator explicitly opts in — 512 MB is not enough for PyTorch.
    on_render = bool(os.environ.get("RENDER", "").strip())
    force_enable = os.environ.get("BOOTK_ENABLE_RAG", "").strip().lower() in ("1", "true", "yes")
    if on_render and not force_enable:
        return
    _get_model()


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

    model = _get_model()
    if model is None:
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
        import numpy as np

        emb_q = model.encode([sub_query], normalize_embeddings=True, show_progress_bar=False)
        emb_c = model.encode(flat_chunks, normalize_embeddings=True, show_progress_bar=False)
        sims = np.dot(emb_c, emb_q.T).flatten()
        max_per: Dict[int, float] = defaultdict(float)
        for row, res_idx in enumerate(chunk_meta):
            max_per[res_idx] = max(max_per[res_idx], cosine_to_unit(float(sims[row])))
        for i, r in enumerate(results):
            if i in max_per:
                r["rag_score"] = round(max_per[i], 4)
                r["rag_status"] = "ok"
    except Exception:
        for i in set(chunk_meta):
            if 0 <= i < len(results):
                results[i]["rag_score"] = None
                results[i]["rag_status"] = "embed_failed"

    for r in results:
        r["coverage_score"] = round(effective_coverage(r), 4)


def is_gap(results_with_scores: List[Dict[str, Any]], quality_threshold: float) -> bool:
    """True if no result clears the quality bar using RAG ∪ snippet scores."""
    if not results_with_scores:
        return True
    best = max(effective_coverage(r) for r in results_with_scores)
    return best < float(quality_threshold or 0.0)
