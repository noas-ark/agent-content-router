import logging
import math
import os
import random
import threading
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
import urllib.request
import urllib.parse
import json as _json
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, send_from_directory

from learning import ConversionEvent, get_metrics_store
from search_provider import fetch_search_results, is_search_configured, get_search_provider_name
from rag_coverage import enrich_free_results_with_rag, warm_embedding_model

from deep_research_tasks import (
    FRAMEWORK_URL as DEEP_RESEARCH_FRAMEWORK_URL,
    evaluate_research_completeness,
    plan_search_tasks,
    plan_followup_tasks,
)

logger = logging.getLogger(__name__)

app = Flask(__name__)
MAX_OPTIMIZE_SECONDS = float(os.environ.get("MAX_OPTIMIZE_SECONDS", "30"))
# One Valyu tier-diff probe per gap by default (up to this many per /optimize).
MAX_VALYU_PROBES_PER_REQUEST = max(1, int(os.environ.get("MAX_VALYU_PROBES_PER_REQUEST", "5")))
# Max total fanout rounds (1 = no re-fanout; 2 = one follow-up round if critic says need_more).
MAX_REFANOUT_ROUNDS = max(1, int(os.environ.get("MAX_REFANOUT_ROUNDS", "2")))
# Brave Web Search API allows up to 20 results per request; was previously hard-coded to 5.
BRAVE_WEB_RESULT_COUNT = max(1, min(20, int(os.environ.get("BRAVE_WEB_RESULT_COUNT", "20"))))
COVERAGE_GAP_POLICY = (
    "A sub-query is in 'gap' when the highest free-hit relevance score is below that "
    "sub-query's quality floor (from signals). Relevance = max(RAG max-chunk similarity, "
    "snippet heuristic). 'ok' means a free hit met the floor; otherwise we may run a paid-tier probe."
)
QUERY_FAN_OUT_REF = "https://dejan.ai/blog/query-fan-out-prompt/"
# Planner pattern only (see deep_reasoning_researcher); decomposition uses this when LLM is available.


def _start_rag_model_warm() -> None:
    """Avoid blocking the first /optimize on Hugging Face download + model load (can take minutes)."""

    def _run() -> None:
        try:
            warm_embedding_model()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name="bootk-rag-warm").start()


_start_rag_model_warm()

# ═══════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════

SOURCES = [
    {"name": "Bloomberg",       "price": 3.00, "auth": .95, "topics": ["finance","economics","markets"],   "freshH": 2,  "type": "premium", "domains": ["bloomberg.com", "www.bloomberg.com"],
     "priceSource": "Cloudflare Pay-Per-Crawl",    "priceDetail": "Bloomberg registered with Cloudflare's pay-per-crawl program. 402 response header returns crawler-price: 3.00 USD. Premium financial content, single-article access."},
    {"name": "WSJ",             "price": 2.50, "auth": .93, "topics": ["finance","business","politics"],   "freshH": 4,  "type": "premium", "domains": ["wsj.com", "www.wsj.com"],
     "priceSource": "TollBit registered publisher", "priceDetail": "WSJ is listed in TollBit's publisher catalog at $2.50/article. Pricing verified against TollBit's public rate card. WSJ also has a Microsoft PCM deal but per-article access is TollBit-routed."},
    {"name": "Financial Times", "price": 3.50, "auth": .94, "topics": ["finance","geopolitics","trade"],   "freshH": 3,  "type": "premium", "domains": ["ft.com", "www.ft.com"],
     "priceSource": "RSL license + TollBit",        "priceDetail": "FT publishes RSL terms at ft.com/robots.txt pointing to rsl-license.xml. Pay-per-crawl rate set at $3.50, classified as premium analysis. TollBit acts as merchant of record."},
    {"name": "Reuters",         "price": 0.80, "auth": .88, "topics": ["news","finance","breaking"],       "freshH": 1,  "type": "wire", "domains": ["reuters.com", "www.reuters.com"],
     "priceSource": "TollBit wire tier",            "priceDetail": "Reuters wire content is priced at the budget tier on TollBit — high volume, fast-turnover news. 402 response includes crawler-price: 0.80. Lower price reflects commodity wire distribution model."},
    {"name": "AP",              "price": 0.70, "auth": .87, "topics": ["news","general","breaking"],       "freshH": 1,  "type": "wire", "domains": ["apnews.com", "www.apnews.com"],
     "priceSource": "Cloudflare Pay-Per-Crawl",    "priceDetail": "AP uses Cloudflare's AI Crawl Control. 402 header: crawler-price: 0.70 USD. Slightly cheaper than Reuters; AP distributes syndicated wire broadly and prices for volume AI access."},
    {"name": "NYT",             "price": 1.50, "auth": .91, "topics": ["news","politics","culture"],       "freshH": 6,  "type": "mid", "domains": ["nytimes.com", "www.nytimes.com"],
     "priceSource": "Microsoft PCM",               "priceDetail": "NYT is a launch partner in Microsoft's Publisher Content Marketplace (PCM). Usage-based pricing at ~$1.50/article for AI assistant access. PCM handles identity verification (KYA) and settlement via Stripe."},
    {"name": "TechCrunch",      "price": 0.50, "auth": .82, "topics": ["tech","startups","AI"],            "freshH": 3,  "type": "mid", "domains": ["techcrunch.com", "www.techcrunch.com"],
     "priceSource": "TollBit mid-tier",            "priceDetail": "TechCrunch is in TollBit's standard publisher catalog. Mid-tier price at $0.50. Content is high-volume, topically specific (tech). 402 response negotiated via TollBit's bot authentication layer."},
    {"name": "Brookings",       "price": 0.00, "auth": .89, "topics": ["policy","research","economics"],   "freshH": 72, "type": "free", "domains": ["brookings.edu", "www.brookings.edu"],
     "priceSource": "Open access / no paywall",    "priceDetail": "Brookings Institution publishes all content under open access. No robots.txt restriction on AI crawling. No RSL license required. Free to access — but no freshness guarantee and no 402 flow."},
    {"name": "arXiv",           "price": 0.00, "auth": .87, "topics": ["science","AI","engineering"],      "freshH": 24, "type": "free", "domains": ["arxiv.org"],
     "priceSource": "Open access (Cornell)",       "priceDetail": "arXiv is operated by Cornell University with a fully open-access mandate. All preprints are freely crawlable. No TollBit, no 402, no RSL. High authority for technical queries but no editorial curation or breaking news."},
    {"name": "Wikipedia",       "price": 0.00, "auth": .75, "topics": ["general","reference","history"],   "freshH": 168,"type": "free", "domains": ["wikipedia.org", "en.wikipedia.org"],
     "priceSource": "CC BY-SA license",            "priceDetail": "Wikipedia content is licensed under Creative Commons Attribution-ShareAlike. Freely crawlable and trainable with attribution. No paywall, no 402 response. Lowest freshness of all sources (weekly update cycle)."},
]

# Map search result hostname -> source dict (for matching real articles to our catalog)
def _build_domain_to_source():
    out = {}
    for s in SOURCES:
        for d in s.get("domains", []):
            out[d.lower()] = s
    return out
DOMAIN_TO_SOURCE = _build_domain_to_source()

DOMAIN_BOOST = {
    "financial_analysis": {"Bloomberg": .32, "WSJ": .24, "Financial Times": .28, "Reuters": .12},
    "breaking_news":      {"Reuters": .30, "AP": .27, "Bloomberg": .14, "NYT": .10},
    "tech_product":       {"TechCrunch": .32, "arXiv": .14},
    "explainer":          {"Wikipedia": .22, "Brookings": .17, "arXiv": .20},
    "policy":             {"Brookings": .32, "NYT": .15, "Financial Times": .14},
    "medical_clinical":   {"arXiv": .30, "NYT": .12},
}

REDUNDANT = [["Reuters", "AP"], ["Bloomberg", "Reuters"]]


def _host_from_url(link: str) -> str:
    try:
        host = (urlparse(link).netloc or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _domain_to_label(host: str) -> str:
    """Turn host like bbc.com into a short label (e.g. BBC)."""
    if not host:
        return "Other"
    # strip www and take first part
    base = host.split(".")[0] if host else "other"
    return base.upper() if len(base) <= 5 else base.capitalize()


def _search_results_to_articles(search_results: list) -> list:
    """
    Which articles are shown: every search result is shown as an article to scrape.
    - No filtering by "selected" purchase plan; we show all results from the search provider.
    - For each result: if its domain is in our catalog (DOMAIN_TO_SOURCE), we show that
      source's name and price; otherwise we show a short domain label (e.g. BBC, CNN) and no price (—).
    """
    if not isinstance(search_results, list):
        return []
    out = []
    for r in search_results:
        if not isinstance(r, dict):
            continue
        link = (r.get("link") or r.get("url") or r.get("href") or "").strip()
        host = _host_from_url(link)
        if not link:
            continue
        src = DOMAIN_TO_SOURCE.get(host) if host else None
        if src:
            source_name, price = src["name"], src["price"]
        else:
            source_name = _domain_to_label(host) if host else "Other"
            price = None
        out.append({
            "title": _text_field(r.get("title")) or "(No title)",
            "url": link,
            "source_name": source_name,
            "price": price,
            "snippet": _text_field(r.get("snippet")),
        })
    return out


# ═══════════════════════════════════════════════════════════════
# SCORING LOGIC
# ═══════════════════════════════════════════════════════════════

def cos_sim(a, b):
    wa = set(w for w in a.lower().split() if len(w) > 2)
    wb = set(w for w in b.lower().split() if len(w) > 2)
    if not wa or not wb:
        return 0
    inter = len(wa & wb)
    return inter / math.sqrt(len(wa) * len(wb))


# Named-entity extraction: spaCy statistical NER (same family of tooling as production routers;
# see https://spacy.io/models/en#en_core_web_sm). Regex only if spaCy / model unavailable.
_SPACY_NLP = None  # lazy: loaded Language, False if load failed, None = not attempted

_NER_LABELS = frozenset(
    {
        "PERSON",
        "NORP",
        "FAC",
        "ORG",
        "GPE",
        "LOC",
        "PRODUCT",
        "EVENT",
        "WORK_OF_ART",
        "LAW",
        "LANGUAGE",
        "DATE",
    }
)


def _is_vague_temporal_for_linking(text: str) -> bool:
    """
    True if the span is a relative / deictic time phrase, not a linkable named entity.
    spaCy often labels these as DATE; they should not appear under "entity linking".
    """
    t = text.strip().casefold()
    if not t:
        return True
    if t in _VAGUE_TIME_EXACT:
        return True
    if _VAGUE_TIME_ANYWHERE.search(f" {t} "):
        return True
    return False


# Whole-string or substring matches for "last year", "past 3 months", etc.
_VAGUE_TIME_EXACT = frozenset(
    {
        "last year",
        "this year",
        "next year",
        "last week",
        "this week",
        "next week",
        "last month",
        "this month",
        "next month",
        "last quarter",
        "this quarter",
        "next quarter",
        "yesterday",
        "today",
        "tomorrow",
        "tonight",
        "last night",
        "this morning",
        "right now",
        "recently",
        "lately",
        "currently",
    }
)

_VAGUE_TIME_ANYWHERE = re.compile(
    r"\b(?:"
    r"last\s+year|this\s+year|next\s+year|"
    r"last\s+week|this\s+week|next\s+week|"
    r"last\s+month|this\s+month|next\s+month|"
    r"last\s+quarter|this\s+quarter|next\s+quarter|"
    r"yesterday|today|tomorrow|tonight|recently|lately|currently|"
    r"last\s+night|this\s+morning|right\s+now|"
    r"(?:last|this|next|past|coming)\s+(?:year|week|month|quarter|day|night|summer|winter|spring|fall|autumn)\b|"
    r"(?:last|next|this)\s+\d{1,3}\s+(?:hours?|days?|weeks?|months?|years?)\b|"
    r"\d+\s+(?:years?|months?|weeks?|days?)\s+ago"
    r")\b",
    re.IGNORECASE,
)


def _get_spacy_nlp():
    """
    Lazy-load spaCy NER once. Tries `import en_core_web_sm` first (works when the model
    wheel is installed via pip); then `spacy.load("en_core_web_sm")`.

    If both fail, the usual cause is: `pip install` without the `en_core_web_sm` package
    (see requirements.txt). Install deps and check logs for the underlying exception.
    """
    global _SPACY_NLP
    if _SPACY_NLP is not None:
        return None if _SPACY_NLP is False else _SPACY_NLP
    err_chain = []
    try:
        import en_core_web_sm  # type: ignore

        _SPACY_NLP = en_core_web_sm.load()
        logger.info("spaCy NER: loaded en_core_web_sm")
        return _SPACY_NLP
    except Exception as e:
        err_chain.append(f"en_core_web_sm.load(): {e!r}")
    try:
        import spacy  # type: ignore

        _SPACY_NLP = spacy.load("en_core_web_sm")
        logger.info("spaCy NER: loaded via spacy.load('en_core_web_sm')")
        return _SPACY_NLP
    except Exception as e:
        err_chain.append(f"spacy.load(): {e!r}")
    _SPACY_NLP = False
    logger.warning(
        "spaCy NER unavailable — entity extraction falls back to years only (%s). "
        "Fix: pip install -r requirements.txt (includes en_core_web_sm wheel).",
        "; ".join(err_chain),
    )
    return None


def _extract_entities_fallback_no_ner(text: str) -> list:
    """
    When the statistical NER model is missing, we do not fake entities with capital-letter
    regex (that misses lowercase names and invents false positives). Only calendar years.
    """
    out = []
    for y in re.findall(r"\b(20\d{2}|19\d{2})\b", text):
        if y not in out:
            out.append(y)
    return out


def extract_named_entities(text: str) -> list:
    """
    Return ordered, de-duplicated entity strings for routing / UI.
    Prefer spaCy NER spans (ORG, PERSON, GPE, …); add 4-digit years if not already covered.
    """
    text = (text or "").strip()
    if not text:
        return []
    nlp = _get_spacy_nlp()
    if nlp:
        doc = nlp(text)
        seen: set = set()
        out: list = []
        for ent in doc.ents:
            if ent.label_ not in _NER_LABELS:
                continue
            t = ent.text.strip()
            if len(t) < 2:
                continue
            if ent.label_ == "DATE" and _is_vague_temporal_for_linking(t):
                continue
            k = t.casefold()
            if k in seen:
                continue
            seen.add(k)
            out.append(t)
        for m in re.finditer(r"\b(20\d{2}|19\d{2})\b", text):
            y = m.group(1)
            if y.casefold() not in seen:
                seen.add(y.casefold())
                out.append(y)
        return out
    return _extract_entities_fallback_no_ner(text)


def extract_signals(query):
    """
    Semantic-router signal bundle for one query (used by /optimize UI and learning).

    Emits:
    - queryUnderstanding: purchase_intent (content type, domain, freshness need, quality floor),
      entity_linking (spaCy NER spans), intent_template, trending_signal, query_cluster,
      routing_rules_fired, tier_strategy
    - intent + intentScores (keyword cosine vs fixed intent profile bags)
    - entities (same strings as entity_linking)
    - relevance: semantic, entityDensity, specificity, templateBoost, composed
    - credibility: stakes, sensitivity, corroboration, controversy, composed
    - freshness: velocity, temporalMarkers, timeMarkers, eventUrgency, decayRate, composed
    - depth: complexity, depthRequired, questionType (Broder-style), ambiguity, composed
    - qualityThreshold, minSources, maxFreshnessHours

    vLLM-style “semantic routers” elsewhere often add embedding similarity and LLM-scored
    intent; this stack is mostly lexical/heuristic + NER by design.
    """
    q = query.lower()
    words = q.split()
    entities = extract_named_entities(query)

    # ── Intent classification ──────────────────────────────────
    intent_profiles = {
        "financial_analysis": "earnings revenue profit stock market investment quarterly financial economics gdp tariff semiconductor fund",
        "breaking_news":      "today latest breaking just announced hours minutes update urgent happened morning",
        "tech_product":       "product launch release features review specs benchmark model gpt llm capabilities version",
        "explainer":          "how does explain history background context overview understand mechanism works",
        "policy":             "regulation law policy act eu government legislation compliance requirement providers",
        "medical_clinical":   "clinical trial drug treatment therapy patient study health symptoms diagnosis results should take",
    }
    intent_scores = {k: cos_sim(q, v) for k, v in intent_profiles.items()}
    sorted_intents = sorted(intent_scores.items(), key=lambda x: -x[1])
    intent = sorted_intents[0][0]
    top_intent_score = sorted_intents[0][1]
    # "What happened in X" / entity-heavy ambiguous -> prefer news/wire
    has_entity = bool(entities) or bool(re.search(r"\b[A-Z][a-z]{2,}\b", query))
    what_happened = bool(re.search(r"\bwhat('s|\s+is|\s+happened|\s+happening)\b", q))
    # Override: (1) "what happened/happening" implies news even with lowercase "iran"; (2) ambiguous + entity
    if what_happened or (top_intent_score < 0.12 and has_entity):
        intent = "breaking_news"
        top_intent_score = 0.5
    semantic_raw = min(top_intent_score * 3.8 + 0.22, 0.98)

    # ── DIMENSION 1: RELEVANCE ────────────────────────────────
    entity_density_raw = min(len(entities) / 7, 1.0)

    specific_markers = r'\b(q[1-4]|20[2-9]\d|\$[\d]+|percent|%|basis\s*points|ipo|ceo|cfo|merger|acquisition|exactly|specific|detail|result)\b'
    specific_triggered = bool(re.search(specific_markers, q, re.IGNORECASE))
    specificity_raw = 0.88 if specific_triggered else (0.65 if len(words) > 9 else 0.38)

    templates = [
        (r'\b(earnings|revenue|profit)\b.*\b(q[1-4]|quarter|annual)\b', "<company>_earnings_<period>", 0.22),
        (r'\b(what\s+did|said|announced|statement)\b',                   "<speaker>_statement",         0.18),
        (r'\b(clinical\s+trial|phase\s+[123]|fda)\b',                    "<medical_trial>",              0.20),
        (r'\b(today|this\s+morning|just|breaking)\b',                    "<breaking_event>",             0.15),
        (r'\b(compliance|regulation|act|law|requirement)\b',             "<policy_query>",               0.12),
        (r'\b(should\s+i|should\s+we)\b',                                "<decision_query>",             0.10),
    ]
    matched_template = None
    for pattern, label, boost in templates:
        if re.search(pattern, query, re.IGNORECASE):
            matched_template = {"label": label, "boost": boost}
            break
    template_boost_raw = 0.5 + (matched_template["boost"] if matched_template else 0)

    relevance_composed = min(0.38*semantic_raw + 0.25*entity_density_raw + 0.22*specificity_raw + 0.15*template_boost_raw, 0.99)

    # ── DIMENSION 2: CREDIBILITY ──────────────────────────────
    high_stakes_pat = r'\b(should\s+i|should\s+we|invest|buy|sell|treatment|diagnosis|legal|liability|compliance|prescription|recommend)\b'
    med_stakes_pat  = r'\b(impact|affect|influence|result|consequence|implication|effect)\b'
    if re.search(high_stakes_pat, q, re.IGNORECASE):
        stakes_raw   = 0.95
        stakes_level = "high"
    elif re.search(med_stakes_pat, q, re.IGNORECASE):
        stakes_raw   = 0.68
        stakes_level = "medium"
    else:
        stakes_raw   = 0.38
        stakes_level = "low"

    sensitivity_pat = r'\b(medical|clinical|legal|financial\s+advice|investment\s+advice|drug|diagnosis|prescription|liability|should\s+i\s+take)\b'
    if re.search(sensitivity_pat, q, re.IGNORECASE):
        sensitivity_raw   = 0.92
        sensitivity_level = "high"
    elif re.search(r'\b(finance|earnings|revenue|profit)\b', q, re.IGNORECASE):
        sensitivity_raw   = 0.72
        sensitivity_level = "finance"
    else:
        sensitivity_raw   = 0.30
        sensitivity_level = "general"

    controversy_pat     = r'\b(policy|regulation|debate|controversial|ban|restrict|versus|vs\.|disagree|dispute|different\s+views)\b'
    controversy_triggered = bool(re.search(controversy_pat, q, re.IGNORECASE))
    controversy_raw     = 0.78 if controversy_triggered else 0.28

    corroboration_raw = min(stakes_raw*0.5 + controversy_raw*0.3 + (0.25 if intent == "breaking_news" else 0), 1.0)

    credibility_composed = min(0.38*stakes_raw + 0.28*sensitivity_raw + 0.22*corroboration_raw + 0.12*controversy_raw, 0.99)

    # ── DIMENSION 3: FRESHNESS ────────────────────────────────
    now_pat    = r'\b(today|this\s+morning|just|breaking|right\s+now|announced|hours\s+ago|minutes\s+ago|tonight|yesterday)\b'
    recent_pat = r'\b(this\s+week|this\s+month|latest|recent|new|2025|2026|q[1-4]\s*202[456])\b'
    archive_pat= r'\b(history|background|how\s+does|explain|what\s+is|overview|2020|2019|2018|originally)\b'

    if re.search(now_pat, q, re.IGNORECASE):
        velocity_raw   = 1.0
        velocity_level = "real-time"
    elif re.search(recent_pat, q, re.IGNORECASE):
        velocity_raw   = 0.74
        velocity_level = "recent"
    elif re.search(archive_pat, q, re.IGNORECASE):
        velocity_raw   = 0.12
        velocity_level = "archival"
    else:
        velocity_raw   = 0.38
        velocity_level = "neutral"

    time_markers = []
    if re.search(now_pat, q, re.IGNORECASE):
        time_markers.append("real-time (<4h)")
    if re.search(recent_pat, q, re.IGNORECASE):
        time_markers.append("recent (<7d)")
    if re.search(r'\b(q[1-4])\b', q, re.IGNORECASE):
        time_markers.append("quarterly")
    if re.search(r'\b(202[3456])\b', q):
        time_markers.append("year-specific")
    temporal_raw = min(velocity_raw + len(time_markers)*0.04, 1.0)

    event_pat        = r'\b(earnings|ipo|merger|acquisition|rate\s+decision|vote|election|launch|announcement|profit\s+warning|down\s+\d|up\s+\d)\b'
    event_triggered  = bool(re.search(event_pat, q, re.IGNORECASE))
    event_urgency_raw = 0.82 if event_triggered else 0.22

    half_life_map = {
        "financial_analysis": 0.88,
        "breaking_news":      1.0,
        "tech_product":       0.58,
        "explainer":          0.10,
        "policy":             0.42,
        "medical_clinical":   0.36,
    }
    decay_raw = half_life_map.get(intent, 0.40)

    freshness_composed = min(0.42*velocity_raw + 0.26*temporal_raw + 0.22*event_urgency_raw + 0.10*decay_raw, 0.99)
    freshness_required = freshness_composed > 0.52
    if freshness_required:
        max_freshness_hours = 4 if velocity_raw >= 0.9 else 12
    elif freshness_composed > 0.4:
        max_freshness_hours = 48
    else:
        max_freshness_hours = 9999

    # ── DIMENSION 4: DEPTH ────────────────────────────────────
    analytical_pat      = r'\b(analyze|analysis|impact|implication|compare|versus|tradeoff|why\s+is|explain\s+why|how\s+does|what\s+are\s+the)\b'
    analytical_triggered = bool(re.search(analytical_pat, q, re.IGNORECASE))
    complexity_raw = min(0.60*(0.88 if analytical_triggered else 0.35) + 0.40*(len(words)/18), 0.99)

    depth_pat             = r'\b(comprehensive|detailed|in-depth|full\s+analysis|thorough|breakdown|deep\s+dive|specific|exactly)\b'
    depth_keywords_triggered = bool(re.search(depth_pat, q, re.IGNORECASE))
    if depth_keywords_triggered:
        depth_required = 0.92
    elif complexity_raw > 0.62:
        depth_required = 0.72
    else:
        depth_required = 0.32

    nav_pat  = r'\b(official|site|website|page|homepage|portal)\b'
    trans_pat= r'\b(how\s+to|steps\s+to|guide|tutorial|sign\s+up)\b'
    if re.search(nav_pat, q, re.IGNORECASE):
        question_type = "navigational"
    elif re.search(trans_pat, q, re.IGNORECASE):
        question_type = "transactional"
    else:
        question_type = "informational"
    question_type_score = {"informational": 0.65, "navigational": 0.30, "transactional": 0.48}[question_type]

    ambiguity_pat      = r'\b(or|versus|vs\.|either|unclear|depends|different\s+views|perspective|both\s+sides)\b'
    ambiguity_triggered = bool(re.search(ambiguity_pat, q, re.IGNORECASE))
    ambiguity_raw = 0.76 if ambiguity_triggered else 0.24

    depth_composed = min(0.40*complexity_raw + 0.32*depth_required + 0.18*question_type_score + 0.10*ambiguity_raw, 0.99)

    # ── Derived thresholds ────────────────────────────────────
    quality_threshold = min(0.60 + credibility_composed*0.30 + depth_composed*0.08, 0.96)
    min_sources = 2 if corroboration_raw > 0.60 else 1

    # ── Facebook-paper style: Query Understanding Stack (Fig 3) ──
    # Content type needed (purchase intent)
    content_type_map = {
        "breaking_news": "real-time news",
        "financial_analysis": "analysis + data",
        "tech_product": "product/review content",
        "explainer": "background / reference",
        "policy": "policy / regulatory",
        "medical_clinical": "clinical / medical",
    }
    freshness_requirement = (
        "real-time" if velocity_raw >= 0.9 else
        "24h" if velocity_raw >= 0.6 else
        "7days" if velocity_raw >= 0.3 else "evergreen"
    )
    topical_domain = intent.replace("_", " ")  # intent doubles as primary domain
    if "tariff" in q or "trade" in q or "geopolit" in q or "policy" in q:
        topical_domain = topical_domain + " + geopolitics"
    if re.search(r"\b(earnings|revenue|profit|q[1-4])\b", q):
        topical_domain = topical_domain + " + earnings"

    # Trending detection (FB §3.1): heuristic = breaking + real-time
    trending_signal = intent == "breaking_news" and velocity_raw >= 0.9

    # Query cluster: richer segment for routing/learning (e.g. financial_earnings_geopolitical)
    cluster_parts = [intent]
    if matched_template and matched_template.get("label"):
        cluster_parts.append(matched_template["label"].replace("<", "").replace(">", "").replace("_", ""))
    if controversy_triggered:
        cluster_parts.append("multi_perspective")
    query_cluster = "_".join(cluster_parts)[:48]

    # Routing rules fired (decision flow that drives tier/source selection)
    routing_rules_fired = []
    if freshness_required and velocity_raw >= 0.9:
        routing_rules_fired.append("premium_real_time")
    if corroboration_raw > 0.60:
        routing_rules_fired.append("corroboration_required")
    if credibility_composed > 0.75:
        routing_rules_fired.append("authoritative_required")
    if intent in ("financial_analysis", "medical_clinical", "policy") and sensitivity_raw > 0.5:
        routing_rules_fired.append("domain_specialist_preferred")
    if not freshness_required and velocity_raw < 0.4:
        routing_rules_fired.append("free_first_ok")
    if depth_required > 0.7:
        routing_rules_fired.append("depth_required")

    # Tier strategy (which content tiers we consider)
    if quality_threshold >= 0.88 and (freshness_required or intent in ("financial_analysis", "medical_clinical")):
        tier_strategy = "premium_required"
    elif not freshness_required and quality_threshold < 0.75:
        tier_strategy = "free_first_then_mid"
    else:
        tier_strategy = "balanced_premium_and_mid"

    query_understanding = {
        "purchase_intent": {
            "content_type_needed": content_type_map.get(intent, intent),
            "topical_domain": topical_domain.strip(),
            "freshness_requirement": freshness_requirement,
            "quality_threshold": round(quality_threshold, 3),
        },
        "entity_linking": entities,
        "intent_template": matched_template["label"] if matched_template else None,
        "trending_signal": trending_signal,
        "query_cluster": query_cluster,
        "routing_rules_fired": routing_rules_fired,
        "tier_strategy": tier_strategy,
    }

    return {
        "queryUnderstanding": query_understanding,
        "intent":          intent,
        "intentScores":    intent_scores,
        "entities":        entities,
        "matchedTemplate": matched_template,
        "relevance": {
            "semantic":         semantic_raw,
            "entityDensity":    entity_density_raw,
            "specificity":      specificity_raw,
            "specificTriggered": specific_triggered,
            "wordCount":        len(words),
            "templateBoost":    template_boost_raw,
            "composed":         relevance_composed,
        },
        "credibility": {
            "stakes":             stakes_raw,
            "stakesLevel":        stakes_level,
            "sensitivity":        sensitivity_raw,
            "sensitivityLevel":   sensitivity_level,
            "corroboration":      corroboration_raw,
            "controversy":        controversy_raw,
            "controversyTriggered": controversy_triggered,
            "composed":           credibility_composed,
        },
        "freshness": {
            "velocity":          velocity_raw,
            "velocityLevel":     velocity_level,
            "temporalMarkers":   temporal_raw,
            "timeMarkers":       time_markers,
            "eventUrgency":      event_urgency_raw,
            "eventTriggered":    event_triggered,
            "decayRate":         decay_raw,
            "composed":          freshness_composed,
            "required":          freshness_required,
            "maxFreshnessHours": max_freshness_hours,
        },
        "depth": {
            "complexity":             complexity_raw,
            "analyticalTriggered":    analytical_triggered,
            "wordCount":              len(words),
            "depthRequired":          depth_required,
            "depthKeywordsTriggered": depth_keywords_triggered,
            "questionType":           question_type,
            "questionTypeScore":      question_type_score,
            "ambiguity":              ambiguity_raw,
            "ambiguityTriggered":     ambiguity_triggered,
            "composed":               depth_composed,
        },
        "qualityThreshold": quality_threshold,
        "minSources":       min_sources,
        "maxFreshnessHours": max_freshness_hours,
    }


def score_source(sigs, src, learned_boost=None):
    intent     = sigs["intent"]
    freshness  = sigs["freshness"]
    credibility = sigs["credibility"]

    topic_text = " ".join(src["topics"])
    semantic   = min(cos_sim(intent.replace("_", " "), topic_text) * 3.2 + 0.28, 0.96)
    authority  = src["auth"]

    f_fit = 0.78
    if freshness["required"]:
        if src["freshH"] <= 4:
            f_fit = 1.0
        elif src["freshH"] <= 12:
            f_fit = 0.55
        elif src["freshH"] <= 24:
            f_fit = 0.28
        else:
            f_fit = 0.05
    elif freshness["composed"] > 0.4:
        f_fit = 0.90 if src["freshH"] <= 48 else 0.72

    # Hard penalty: freshness required + source is structurally stale (free sources)
    if freshness["required"] and src["price"] == 0:
        f_fit *= 0.25

    # Blend static domain boost with learned publisher performance (citation rate / value)
    boost = DOMAIN_BOOST.get(intent, {}).get(src["name"], 0)
    if learned_boost:
        boost = min(0.98, boost + learned_boost.get(src["name"], 0))
    q_fit = (
        {"premium": 1.0, "mid": 0.82, "wire": 0.76, "free": 0.52}.get(src["type"], 0.6)
        if credibility["composed"] > 0.70
        else 1.0
    )

    utility = min(0.28*semantic + 0.24*authority + 0.24*f_fit + 0.14*(0.5+boost) + 0.10*q_fit, 0.99)
    return {
        "semantic":     semantic,
        "authority":    authority,
        "freshnessFit": f_fit,
        "domainBoost":  0.5 + boost,
        "qFit":         q_fit,
        "utility":      utility,
    }


INTENT_BASE = {
    "prior_art": 1.00,
    "regulatory": 0.75,
    "analysis": 0.50,
    "breaking_news": 0.35,
    "historical": 0.00,
    "factual_lookup": 0.00,
}


def _env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _instructor_client():
    api_key = _env_first("DSAIL_OPENAI_API_KEY", "OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        import instructor  # type: ignore
        from openai import OpenAI  # type: ignore
        return instructor.from_openai(OpenAI(api_key=api_key))
    except Exception:
        return None


def _extract_entities_local(text: str) -> list:
    """Same NER path as extract_signals (spaCy + regex fallback)."""
    return extract_named_entities(text)


def _routing_confidence(keyword_hits, domain, intent) -> float:
    conf = 0.2
    if keyword_hits:
        conf += 0.3
    if domain != "general":
        conf += 0.25
    if intent != "factual_lookup":
        conf += 0.25
    return min(conf, 1.0)


def compute_bid_ceiling(sigs: dict) -> float:
    base = INTENT_BASE.get(sigs.get("intent"), 0.25)
    if sigs.get("domain") == "medical" and sigs.get("quality_threshold", 0) >= 0.85:
        base = max(base, 0.75)
    if sigs.get("domain") == "legal" and sigs.get("intent") in ("prior_art", "regulatory"):
        base = max(base, 0.60)
    multiplier = 0.6 + sigs.get("complexity_score", 0.5) * 0.4
    if sigs.get("quality_threshold", 0.7) < 0.7:
        multiplier *= 0.5
    return round(min(base * multiplier, 2.0), 4)


def _infer_signals_local(query: str, goal: str) -> dict:
    text = f"{query} {goal}".lower()
    entities = _extract_entities_local(f"{query} {goal}")
    keyword_groups = {
        "finance": ["revenue", "earnings", "market", "stock", "tariff", "gdp"],
        "medical": ["trial", "drug", "clinical", "patient", "fda"],
        "legal": ["regulation", "law", "compliance", "act", "court"],
        "news": ["today", "latest", "breaking", "announced", "update"],
    }
    keyword_hits = [k for k, terms in keyword_groups.items() if any(t in text for t in terms)]
    if "news" in keyword_hits:
        intent = "breaking_news"
    elif "legal" in keyword_hits:
        intent = "regulatory"
    elif "medical" in keyword_hits:
        intent = "analysis"
    elif "finance" in keyword_hits:
        intent = "analysis"
    elif any(w in text for w in ["history", "background", "overview"]):
        intent = "historical"
    else:
        intent = "factual_lookup"

    if "medical" in keyword_hits:
        domain = "medical"
    elif "legal" in keyword_hits:
        domain = "legal"
    elif "finance" in keyword_hits:
        domain = "financial"
    else:
        domain = "general"
    complexity_score = min(1.0, (len(query.split()) + len(goal.split())) / 28)
    requires_freshness = "news" in keyword_hits
    quality_threshold = 0.9 if intent in ("prior_art", "regulatory") else (0.82 if intent in ("analysis", "breaking_news") else 0.65)
    content_type_needed = (
        "news_article" if intent == "breaking_news"
        else "regulatory_doc" if intent == "regulatory"
        else "academic_paper" if domain == "medical"
        else "market_data" if domain == "financial"
        else "primary_source"
    )
    signals = {
        "query": query,
        "goal": goal,
        "intent": intent,
        "task_type": "synthesis",
        "domain": domain,
        "sub_domain": None,
        "entities": entities,
        "intent_template": None,
        "requires_freshness": requires_freshness,
        "content_type_needed": content_type_needed,
        "quality_threshold": round(quality_threshold, 3),
        "complexity_score": round(complexity_score, 3),
        "keyword_hits": keyword_hits,
        "routing_confidence": round(_routing_confidence(keyword_hits, domain, intent), 3),
        "valyu_sources": [],
    }
    signals["max_price_usd"] = compute_bid_ceiling(signals)
    signals["price_derivation"] = {
        "intent_base": INTENT_BASE.get(intent, 0.25),
        "complexity_multiplier": round(0.6 + complexity_score * 0.4, 3),
        "quality_threshold": signals["quality_threshold"],
        "max_price_usd": signals["max_price_usd"],
    }
    return signals


def _current_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%B %d, %Y")


def _query_complexity(query: str) -> int:
    """
    Estimate how many sub-queries are appropriate for a given query.
    Returns an int in [3, 6]: 3 for simple queries, up to 6 for long multi-facet ones.
    """
    words = query.split()
    word_score = (
        0 if len(words) <= 8
        else (1 if len(words) <= 15
              else (2 if len(words) <= 25
                    else 3))
    )
    entities = _extract_entities_local(query)
    entity_score = 0 if len(entities) <= 1 else (1 if len(entities) <= 3 else 2)
    conj = len(re.findall(r"\band\b|\bor\b", query, re.IGNORECASE))
    extra_q = max(0, query.count("?") - 1)
    multi_score = min(2, conj + extra_q)
    total = word_score + entity_score + multi_score
    return 3 if total <= 1 else (4 if total <= 3 else (5 if total <= 5 else 6))


def _local_research_objective(original: str, facet_query: str, facet_goal: str) -> str:
    """DeepReason-style expanded intent for local decomposition (no LLM)."""
    o = original.strip()
    head = o[:420] + ("…" if len(o) > 420 else "")
    return (
        f"Support the parent question (“{head}”) by executing this web search. "
        f"Facet focus: {facet_goal}. "
        f"Gather verifiable facts, entities, and time-bounded claims from results that would let "
        f"a researcher answer that question—note how this facet composes with other sub-queries."
    )


def _decompose_query_local(query: str, n: int = 3) -> dict:
    """Heuristic fan-out when Instructor/OpenAI is unavailable (DEJAN-style behavior)."""
    q = query.strip()
    tokens = [w for w in re.split(r"\s+", q) if w]
    date_s = _current_date_str()
    rationale = (
        f"Local fan-out (no LLM): prefer minimal queries; one aspect per string. "
        f"Date context: {date_s}. Original topic: {q[:200]}{'…' if len(q) > 200 else ''}"
    )
    if len(tokens) <= 10:
        g0 = "Single search covering the full question."
        return {
            "sub_queries": [
                {
                    "query": q,
                    "goal": g0,
                    "research_objective": _local_research_objective(q, q, g0),
                }
            ],
            "rationale": rationale,
            "source": "local",
        }
    chunks = []
    step = max(3, len(tokens) // min(n, max(2, len(tokens) // 4)))
    facet_i = 0
    for i in range(0, len(tokens), step):
        part = " ".join(tokens[i : i + step]).strip()
        if part:
            facet_i += 1
            g = part if len(part) <= 160 else part[:157] + "…"
            gl = f"Facet {facet_i}: {g}"
            chunks.append(
                {
                    "query": part,
                    "goal": gl,
                    "research_objective": _local_research_objective(q, part, gl),
                }
            )
    sub = chunks[:n] or [
        {
            "query": q,
            "goal": "Single search covering the full question.",
            "research_objective": _local_research_objective(q, q, "Single search covering the full question."),
        }
    ]
    return {"sub_queries": sub, "rationale": rationale, "source": "local"}


def _decompose_query(query: str, n: int = 3) -> dict:
    """
    Targeted search tasks via the deep_reasoning_researcher *planner* pattern (distinct web
    queries per facet). Falls back to local heuristics when no LLM.
    Returns { sub_queries: [{query, goal, research_objective}], rationale: str, source: str }.
    """
    client = _instructor_client()
    if client is not None:
        dr = plan_search_tasks(query, n, client)
        if dr:
            return dr
    return _decompose_query_local(query, n=n)


def _decompose_followup_queries(
    original_query: str,
    already_tried: list[str],
    n: int = 3,
) -> dict:
    """
    Like _decompose_query but for follow-up rounds — instructs the planner to avoid
    already-tried queries. Falls back to _decompose_query_local if LLM unavailable.
    """
    client = _instructor_client()
    if client is not None:
        dr = plan_followup_tasks(original_query, already_tried, n, client)
        if dr:
            return dr
    return _decompose_query_local(original_query, n=n)


def _build_research_digest(sub_query_runs: list) -> str:
    """Titles/snippets only — what the critic judges (no full article body)."""
    parts: list[str] = []
    for run in sub_query_runs:
        if not isinstance(run, dict):
            continue
        parts.append(f"=== Sub-query {run.get('index', '?')}: {run.get('query', '')} ===")
        for r in (run.get("free_results") or [])[:10]:
            if not isinstance(r, dict):
                continue
            title = _text_field(r.get("title"), 220)
            snip = _text_field(r.get("snippet"), 400)
            parts.append(f"- {title}\n  {snip}")
    return "\n".join(parts)


def _text_field(val, max_len: int | None = None) -> str:
    """Coerce search/API fields to plain str for JSON/UI. Prevents odd types (e.g. slice) from appearing as text."""
    if val is None:
        s = ""
    elif isinstance(val, slice):
        s = ""
    elif isinstance(val, bytes):
        s = val.decode("utf-8", errors="replace")
    elif isinstance(val, str):
        s = val
    else:
        s = str(val)
    s = s.replace("\n", " ").strip()
    if max_len is not None and len(s) > max_len:
        s = s[: max_len]
    return s


def _infer_signals(query: str, goal: str) -> dict:
    client = _instructor_client()
    local = _infer_signals_local(query, goal)
    if client is None:
        return local
    try:
        from pydantic import BaseModel
        from typing import Literal

        class SignalModel(BaseModel):
            intent: Literal["prior_art", "factual_lookup", "analysis", "breaking_news", "historical", "regulatory"]
            task_type: Literal["synthesis", "validation", "comparison", "discovery", "factual_lookup"]
            domain: Literal["scientific", "legal", "financial", "medical", "competitive", "general"]
            sub_domain: str | None = None
            intent_template: str | None = None
            requires_freshness: bool
            content_type_needed: Literal[
                "academic_paper",
                "news_article",
                "regulatory_doc",
                "market_data",
                "primary_source",
                "case_law",
            ]
            quality_threshold: float

        prompt = (
            "Extract structured routing signals for this research sub-query.\n"
            f"Query: {query}\n"
            f"Goal: {goal}\n"
            "Use the schema fields only. Keep quality_threshold in [0,1]."
        )
        model_out = client.chat.completions.create(
            model="gpt-4o-mini",
            response_model=SignalModel,
            messages=[
                {"role": "system", "content": "You are a query understanding system for content routing."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            timeout=8,
        )
        signals = {
            **local,
            "intent": model_out.intent,
            "task_type": model_out.task_type,
            "domain": model_out.domain,
            "sub_domain": model_out.sub_domain,
            "intent_template": model_out.intent_template,
            "requires_freshness": bool(model_out.requires_freshness),
            "content_type_needed": model_out.content_type_needed,
            "quality_threshold": max(0.0, min(1.0, float(model_out.quality_threshold))),
        }
        signals["max_price_usd"] = compute_bid_ceiling(signals)
        signals["price_derivation"] = {
            "intent_base": INTENT_BASE.get(signals["intent"], 0.25),
            "complexity_multiplier": round(0.6 + signals.get("complexity_score", 0.5) * 0.4, 3),
            "quality_threshold": signals["quality_threshold"],
            "max_price_usd": signals["max_price_usd"],
            "source": "instructor+local-derivation",
        }
        return signals
    except Exception:
        return local


def _brave_search(query: str, count=None):
    if count is None:
        count = BRAVE_WEB_RESULT_COUNT
    key = _env_first("DSAIL_BRAVE_API_KEY", "BRAVE_API_KEY")
    if not key:
        return []
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": count, "search_lang": "en"}
    )
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": key,
            "User-Agent": "bootk-ai-router/4.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            payload = _json.loads(resp.read().decode())
        results = (payload.get("web") or {}).get("results") or []
        return [{
            "title": _text_field(r.get("title"), 800),
            "url": _text_field(r.get("url"), 4000),
            "snippet": _text_field(r.get("description"), 8000),
            "source": _text_field(
                ((r.get("profile") or {}).get("name") or _host_from_url(r.get("url", ""))),
                300,
            ),
            "date": r.get("page_age"),
        } for r in results[:count]]
    except Exception:
        return []


def _snippet_coverage_score(result: dict, signals: dict) -> float:
    """Heuristic score from title + snippet only (fallback when RAG fetch unavailable)."""
    text = f"{_text_field(result.get('title'))} {_text_field(result.get('snippet'))}".lower()
    entities = signals.get("entities") or []
    entity_cov = (sum(1 for e in entities if str(e).lower() in text) / len(entities)) if entities else 0.5
    keyword_hits = signals.get("keyword_hits") or []
    keyword_score = (sum(1 for k in keyword_hits if k.lower() in text) / max(len(keyword_hits), 1))
    temporal = 0.85 if signals.get("requires_freshness") and result.get("date") else 0.7
    return round(entity_cov * 0.4 + temporal * 0.25 + keyword_score * 0.2 + 0.15, 3)


def _to_cpm(per_doc_usd: float) -> int:
    if per_doc_usd <= 0:
        return 0
    if per_doc_usd < 0.01:
        return 2
    if per_doc_usd < 0.05:
        return 10
    if per_doc_usd < 0.20:
        return 30
    return 50


def _infer_price_from_source_url(url: str, cpm_ceiling: int) -> float:
    u = (url or "").lower()
    tier_map = {
        "arxiv.org": 0.0005,
        "pubmed.ncbi.nlm.nih.gov": 0.0005,
        "clinicaltrials.gov": 0.0005,
        "sec.gov": 0.0005,
        "biorxiv.org": 0.0005,
        "medrxiv.org": 0.0005,
    }
    for d, p in tier_map.items():
        if d in u:
            return p
    if any(d in u for d in ["wiley.com", "springer.com", "elsevier.com", "nature.com"]):
        return min(0.05, cpm_ceiling / 1000.0)
    if any(
        d in u
        for d in (
            "nejm.org",
            "jamanetwork.com",
            "thelancet.com",
            "ahajournals.org",
            "acc.org",
        )
    ):
        return min(0.05, cpm_ceiling / 1000.0)
    return min(0.0015, cpm_ceiling / 1000.0)


def _valyu_api_key_present() -> bool:
    return bool(_env_first("DSAIL_VALYU_API_KEY", "VALYU_API_KEY"))


def _empty_probe_record(sub_query: str, *, skipped_reason: str, attempted: bool) -> dict:
    return {
        "sub_query": sub_query,
        "free_count": 0,
        "paid_count": 0,
        "new_at_paid": 0,
        "results": [],
        "skipped_reason": skipped_reason,
        "attempted": attempted,
    }


def _probe_valyu(sub_query: str, signals: dict) -> dict:
    key = _env_first("DSAIL_VALYU_API_KEY", "VALYU_API_KEY")
    if not key:
        return {
            "sub_query": sub_query,
            "free_count": 0,
            "paid_count": 0,
            "new_at_paid": 0,
            "results": [],
            "skipped_reason": None,
            "attempted": True,
        }
    try:
        from valyu import Valyu  # type: ignore
    except Exception:
        return {
            "sub_query": sub_query,
            "free_count": 0,
            "paid_count": 0,
            "new_at_paid": 0,
            "results": [],
            "skipped_reason": None,
            "attempted": True,
        }
    valyu = Valyu(api_key=key)
    raw_ceiling = signals.get("max_price_usd", 0) or 0
    cpm_ceiling = _to_cpm(raw_ceiling)
    # Proprietary index with max_price=0 often returns nothing; use a small floor so tier-diff runs.
    cpm_for_proprietary = max(int(cpm_ceiling), 1)
    err_note: str | None = None
    try:
        # included_sources only applies when signals["valyu_sources"] is non-empty; we do not default to a single publisher.
        free_tier = valyu.search(
            sub_query,
            search_type="all",
            max_price=0,
            max_num_results=5,
            included_sources=signals.get("valyu_sources") or None,
        )
        paid_tier = valyu.search(
            sub_query,
            search_type="proprietary",
            max_price=cpm_for_proprietary,
            max_num_results=5,
            included_sources=signals.get("valyu_sources") or None,
        )
        free_results = list(getattr(free_tier, "results", []) or [])
        paid_results = list(getattr(paid_tier, "results", []) or [])
    except Exception as ex:
        err_note = (str(ex) or type(ex).__name__)[:220]
        return {
            "sub_query": sub_query,
            "free_count": 0,
            "paid_count": 0,
            "new_at_paid": 0,
            "results": [],
            "skipped_reason": None,
            "attempted": True,
            "valyu_error": err_note,
        }

    free_urls = {getattr(r, "url", "") for r in free_results}
    paid_urls = {getattr(r, "url", "") for r in paid_results}
    new_at_paid = paid_urls - free_urls
    out = []
    for r in paid_results:
        url = getattr(r, "url", "")
        catalog = _infer_price_from_source_url(url, cpm_ceiling)
        inc = url in new_at_paid
        out.append({
            "title": _text_field(getattr(r, "title", ""), 800),
            "url": url,
            "snippet": _text_field(getattr(r, "content", ""), 400),
            "source": _host_from_url(url),
            "date": getattr(r, "publication_date", None),
            # Incremental $ only when URL is in proprietary but not in free Valyu index (buy signal).
            "inferred_price": catalog if inc else 0.0,
            "estimated_catalog_usd": round(float(catalog), 6),
            "incremental_paid_only": inc,
            "unlocked_at": "paid" if inc else "free",
        })
    # We only iterated paid_results above; if proprietary returned nothing but the free index had hits,
    # still surface those so the UI shows Valyu responded (tier-diff just has no paid-only URLs).
    only_free_fallback = False
    if not out and free_results:
        only_free_fallback = True
        for r in free_results[:5]:
            url = getattr(r, "url", "")
            catalog = _infer_price_from_source_url(url, cpm_ceiling)
            out.append({
                "title": _text_field(getattr(r, "title", ""), 800),
                "url": url,
                "snippet": _text_field(getattr(r, "content", ""), 400),
                "source": _host_from_url(url),
                "date": getattr(r, "publication_date", None),
                "inferred_price": 0.0,
                "estimated_catalog_usd": round(float(catalog), 6),
                "incremental_paid_only": False,
                "unlocked_at": "free",
            })
    return {
        "sub_query": sub_query,
        "free_count": len(free_results),
        "paid_count": len(paid_results),
        "new_at_paid": len(new_at_paid),
        "results": out,
        "skipped_reason": None,
        "attempted": True,
        "valyu_only_free_tier": only_free_fallback,
        "valyu_proprietary_cpm_used": cpm_for_proprietary,
    }


CONTENT_TYPE_MAP = {
    ("arxiv", "academic_paper"): 1.0,
    ("pubmed", "academic_paper"): 1.0,
    ("sec", "market_data"): 1.0,
    ("sec", "regulatory_doc"): 0.8,
    ("wiley", "academic_paper"): 1.0,
}


def _relevance_score(result: dict, signals: dict) -> float:
    text = f"{_text_field(result.get('title'))} {_text_field(result.get('snippet'))}".lower()
    entities = signals.get("entities") or []
    entity_cov = (sum(1 for e in entities if str(e).lower() in text) / len(entities)) if entities else 0.5
    src = (result.get("source") or "").lower()
    content_needed = signals.get("content_type_needed", "primary_source")
    type_score = 0.5
    for (k_src, k_type), v in CONTENT_TYPE_MAP.items():
        if k_src in src and k_type == content_needed:
            type_score = v
            break
    keyword_hits = signals.get("keyword_hits") or []
    keyword_score = (sum(1 for k in keyword_hits if k.lower() in text) / max(len(keyword_hits), 1))
    temporal = 0.85 if signals.get("requires_freshness") and result.get("date") else 0.7
    return round(entity_cov * 0.35 + keyword_score * 0.25 + type_score * 0.25 + temporal * 0.15, 3)


def _compute_bid(signals: dict, result: dict) -> dict:
    rel = _relevance_score(result, signals)
    bid = round(signals.get("max_price_usd", 0) * rel, 4)
    inferred = result.get("inferred_price", 0)
    return {
        "relevance_score": rel,
        "bid": bid,
        "inferred_price": inferred,
        "decision": "buy" if bid >= inferred else "pass",
        "reasoning": f"ceiling ${signals.get('max_price_usd', 0)} × rel {rel:.2f} = bid ${bid:.4f}",
    }


def _signals_goal_text(sq: dict) -> str:
    """Text for routing signals: short goal + expanded research objective when present."""
    g = (sq.get("goal") or "").strip()
    ro = (sq.get("research_objective") or "").strip()
    if ro and g:
        return f"{g}\n\n{ro}"
    return ro or g


def _run_fanout_round(
    sub_queries: list,
    deadline: float,
    valyu_probes_used: int,
) -> tuple:
    """
    Execute one round of the fanout loop: signals → search → RAG enrich → Valyu probe.
    Returns (sq_with_signals, free_results, probe_results, search_providers, valyu_probes_used).
    """
    sq_with_signals: list = []
    free_results: dict = {}
    probe_results: dict = {}
    search_providers: dict = {}

    for sq in sub_queries:
        sq_query = sq["query"]
        if time.time() > deadline:
            break
        signals = _infer_signals(sq_query, _signals_goal_text(sq))
        sq_with_signals.append((sq, signals))
        provider_used = "Brave"
        free = _brave_search(sq_query, count=BRAVE_WEB_RESULT_COUNT)
        if not free:
            fallback, provider_used = fetch_search_results(sq_query, num=BRAVE_WEB_RESULT_COUNT)
            free = [
                {
                    "title": _text_field(r.get("title"), 800),
                    "url": _text_field(r.get("link"), 4000),
                    "snippet": _text_field(r.get("snippet"), 8000),
                    "source": _text_field(r.get("displayLink"), 300),
                    "date": None,
                }
                for r in fallback
            ]
        search_providers[sq_query] = provider_used
        for r in free:
            r["snippet_score"] = _snippet_coverage_score(r, signals)
            r["rag_score"] = None
            r["rag_status"] = None
        enrich_free_results_with_rag(free, sq_query, deadline)
        free_results[sq_query] = free
        best_free = max([r.get("coverage_score", 0) for r in free], default=0)
        if best_free >= signals["quality_threshold"]:
            probe_results[sq_query] = _empty_probe_record(
                sq_query, skipped_reason="free_tier_met", attempted=False
            )
        elif (
            best_free < signals["quality_threshold"]
            and valyu_probes_used < MAX_VALYU_PROBES_PER_REQUEST
            and time.time() <= deadline
        ):
            probe = _probe_valyu(sq_query, signals)
            probe_results[sq_query] = probe
            valyu_probes_used += 1
        else:
            probe_results[sq_query] = _empty_probe_record(
                sq_query, skipped_reason="probe_budget_exhausted", attempted=False
            )

    return sq_with_signals, free_results, probe_results, search_providers, valyu_probes_used


def _build_sub_query_runs(
    sq_with_signals: list,
    free_results: dict,
    probe_results: dict,
    search_providers: dict,
) -> list:
    """
    Build the sub_query_runs list from accumulated fanout state.
    Indices are 1-based over the full sq_with_signals list.
    """
    sub_query_runs = []
    for idx, (sq, signals) in enumerate(sq_with_signals, start=1):
        sq_query = sq["query"]
        fr = free_results.get(sq_query, [])
        best_cov = max((r.get("coverage_score", 0) for r in fr), default=0.0)
        qth = float(signals.get("quality_threshold", 0) or 0)
        is_gap = best_cov < qth
        gap_detail = (
            f"gap: best free relevance {best_cov:.3f} < quality floor {qth:.3f} "
            f"(raise hit quality or use paid probe)"
            if is_gap
            else f"ok: best free relevance {best_cov:.3f} ≥ floor {qth:.3f}"
        )
        free_out = []
        for r in fr:
            free_out.append(
                {
                    "title": _text_field(r.get("title"), 300),
                    "url": r.get("url") or "",
                    "snippet": _text_field(r.get("snippet"), 400),
                    "source": _text_field(r.get("source"), 300),
                    "date": r.get("date"),
                    "snippet_score": round(float(r.get("snippet_score", 0) or 0), 4),
                    "rag_score": r.get("rag_score"),
                    "rag_status": r.get("rag_status"),
                    "coverage_score": round(float(r.get("coverage_score", 0) or 0), 4),
                }
            )
        pr = probe_results.get(sq_query, {}) or {}
        probe_list = pr.get("results") or []
        probe_out = []
        for r in probe_list:
            if not isinstance(r, dict):
                continue
            probe_out.append(
                {
                    "title": _text_field(r.get("title"), 300),
                    "url": r.get("url") or "",
                    "snippet": _text_field(r.get("snippet"), 400),
                    "source": _text_field(r.get("source"), 300),
                    "date": r.get("date"),
                    "inferred_price": float(r.get("inferred_price", 0) or 0),
                    "estimated_catalog_usd": (
                        round(float(r["estimated_catalog_usd"]), 6)
                        if "estimated_catalog_usd" in r
                        else None
                    ),
                    "incremental_paid_only": r.get("incremental_paid_only"),
                    "unlocked_at": r.get("unlocked_at") or "",
                }
            )
        sub_query_runs.append(
            {
                "index": idx,
                "query": sq_query,
                "goal": sq.get("goal", ""),
                "research_objective": (sq.get("research_objective") or "").strip(),
                "intent": signals.get("intent"),
                "domain": signals.get("domain"),
                "max_price_usd": round(float(signals.get("max_price_usd", 0) or 0), 4),
                "search_provider": search_providers.get(sq_query, "—"),
                "free_results": free_out,
                "best_coverage": round(float(best_cov), 3),
                "quality_threshold": round(qth, 3),
                "coverage_status": "gap" if is_gap else "ok",
                "coverage_gap_detail": gap_detail,
                "probe": {
                    "free_count": int(pr.get("free_count", 0) or 0),
                    "paid_count": int(pr.get("paid_count", 0) or 0),
                    "new_at_paid": int(pr.get("new_at_paid", 0) or 0),
                    "results": probe_out,
                    "skipped_reason": pr.get("skipped_reason"),
                    "attempted": pr.get("attempted"),
                    "valyu_error": pr.get("valyu_error"),
                    "valyu_only_free_tier": pr.get("valyu_only_free_tier"),
                    "valyu_proprietary_cpm_used": pr.get("valyu_proprietary_cpm_used"),
                },
            }
        )
    return sub_query_runs


def _synthesize_research_answer(
    query: str,
    digest: str,
    rc: dict,
    free_plan_n: int,
    paid_plan_n: int,
    total_bid: float,
    client,
) -> dict:
    """
    Writer-node style answer from the same digest the critic sees (deep_reasoning_researcher
    flow: retrieve → critic → writer). Stub when no LLM.
    """
    stub = {
        "mode": "stub",
        "text": (
            "This step corresponds to the **Writer** node in "
            "[deep_reasoning_researcher](https://github.com/themiccc/deep_reasoning_researcher): "
            "after search and the critic, the pipeline would synthesize a full answer from gathered evidence. "
            "Configure an OpenAI API key for Instructor to generate that answer here from your snippets."
        ),
        "summary_line": "Writer step — configure LLM for synthesized answer.",
    }
    if client is None:
        return stub
    if not (digest or "").strip():
        return {**stub, "text": stub["text"] + " (Empty digest — nothing to synthesize.)"}
    try:
        from pydantic import BaseModel, Field

        class ResearchWriterOutput(BaseModel):
            """Instructor requires response_model on patched OpenAI clients."""

            answer: str = Field(
                description=(
                    "Several short paragraphs answering the user question using only the evidence; "
                    "say when information is missing; no invented URLs, quotes, or numbers."
                )
            )

        pct = rc.get("completeness_percent")
        need = rc.get("need_more_information")
        prompt = (
            f"User question:\n{query.strip()}\n\n"
            f"Evidence (web search titles/snippets only):\n{digest[:12000]}\n\n"
            f"Completeness context: ~{pct}% · need_more_information={need} · "
            f"free hits in plan: {free_plan_n} · paid line items: {paid_plan_n} · total bid ${total_bid:.4f}\n\n"
            "Write a clear answer (several short paragraphs). Ground claims in the evidence above; "
            "if something is not in the snippets, say so. Do not invent URLs, quotes, or numbers not implied by the text."
        )
        out = client.chat.completions.create(
            model="gpt-4o-mini",
            response_model=ResearchWriterOutput,
            messages=[
                {
                    "role": "system",
                    "content": "You are the Writer in a research pipeline. Answer only from the user's evidence block.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.25,
            timeout=45,
        )
        text = (out.answer or "").strip()
        return {
            "mode": "llm",
            "text": text,
            "summary_line": "Writer: answer drafted from search digest.",
        }
    except Exception as ex:
        logger.warning("synthesis failed: %s", ex)
        return {
            "mode": "error",
            "text": f"Synthesis failed: {(str(ex) or type(ex).__name__)[:280]}",
            "summary_line": "Writer step failed.",
        }


def optimize(query, customer_id="default"):
    deadline = time.time() + MAX_OPTIMIZE_SECONDS

    # --- Round 1: plan ---
    _n_queries = _query_complexity(query)
    _decomp = _decompose_query(query, n=_n_queries)
    sub_queries = _decomp.get("sub_queries") or []
    query_fan_out_rationale = (_decomp.get("rationale") or "").strip()

    # Accumulated state across all fanout rounds
    sq_with_signals: list = []
    free_results: dict = {}
    probe_results: dict = {}
    search_providers: dict = {}
    all_candidates: list = []

    # --- Round 1: fanout ---
    r_sq, r_free, r_probe, r_prov, valyu_probes_used = _run_fanout_round(
        sub_queries, deadline, 0
    )
    sq_with_signals.extend(r_sq)
    free_results.update(r_free)
    probe_results.update(r_probe)
    search_providers.update(r_prov)

    # --- Re-fanout loop: fire more searches when critic says need_more_information ---
    _refanout_round = 1
    while _refanout_round < MAX_REFANOUT_ROUNDS and time.time() < deadline:
        _snapshot = _build_sub_query_runs(sq_with_signals, free_results, probe_results, search_providers)
        _digest = _build_research_digest(_snapshot)
        _mean_best = (
            sum(r.get("best_coverage", 0) or 0 for r in _snapshot) / len(_snapshot)
            if _snapshot else 0.0
        )
        _interim = evaluate_research_completeness(
            query,
            _digest,
            _instructor_client(),
            {
                "n_subqueries": len(_snapshot),
                "subqueries_met_floor": sum(1 for r in _snapshot if r.get("coverage_status") == "ok"),
                "gap_count": sum(1 for r in _snapshot if r.get("coverage_status") == "gap"),
                "mean_best_relevance": _mean_best,
            },
        )
        if not _interim.get("need_more_information"):
            break
        if time.time() >= deadline:
            break
        already_tried = [sq["query"] for sq, _ in sq_with_signals]
        _followup_n = max(2, _n_queries // 2)
        _followup_decomp = _decompose_followup_queries(query, already_tried, n=_followup_n)
        followup_sqs = _followup_decomp.get("sub_queries") or []
        if not followup_sqs:
            break
        r_sq, r_free, r_probe, r_prov, valyu_probes_used = _run_fanout_round(
            followup_sqs, deadline, valyu_probes_used
        )
        sq_with_signals.extend(r_sq)
        free_results.update(r_free)
        probe_results.update(r_probe)
        search_providers.update(r_prov)
        _refanout_round += 1

    free_plan, paid_plan, skipped = [], [], []
    purchased_urls = set()
    total_bid = 0.0
    covered = 0

    for sq, signals in sq_with_signals:
        sq_query = sq["query"]
        free = free_results.get(sq_query, [])
        best_free = max(free, key=lambda r: r.get("coverage_score", 0), default=None)
        if best_free and best_free.get("coverage_score", 0) >= signals["quality_threshold"]:
            free_plan.append({
                "sub_query": sq_query,
                "source": best_free.get("source") or _domain_to_label(_host_from_url(best_free.get("url", ""))),
                "url": best_free.get("url", ""),
                "title": best_free.get("title", ""),
                "coverage_score": best_free.get("coverage_score", 0),
            })
            covered += 1
            all_candidates.append({
                "name": best_free.get("source") or _domain_to_label(_host_from_url(best_free.get("url", ""))),
                "price": 0.0,
                "utility": min(0.99, best_free.get("coverage_score", 0.5)),
                "our_bid": 0.0,
                "bid_decision": "buy",
                "bid_detail": {"formula": "FREE", "utility": best_free.get("coverage_score", 0.5), "percentile": None},
                "url": best_free.get("url", ""),
            })
            continue

        probe = probe_results.get(sq_query, {})
        candidates = probe.get("results", [])
        bid_candidates = []
        for r in candidates:
            if r.get("unlocked_at") != "paid":
                continue
            bid_eval = _compute_bid(signals, r)
            bid_candidates.append({**r, **bid_eval})
        bid_candidates.sort(key=lambda r: -r["relevance_score"])

        bought = False
        for c in bid_candidates:
            if c["decision"] == "buy" and c.get("url") not in purchased_urls:
                paid_plan.append({
                    "sub_query": sq_query,
                    "source": c.get("source", ""),
                    "url": c.get("url", ""),
                    "title": c.get("title", ""),
                    "inferred_price": c.get("inferred_price", 0),
                    "bid": c.get("bid", 0),
                    "relevance_score": c.get("relevance_score", 0),
                    "reasoning": c.get("reasoning", ""),
                })
                purchased_urls.add(c.get("url"))
                total_bid += c.get("bid", 0)
                covered += 1
                bought = True
                all_candidates.append({
                    "name": _domain_to_label(_host_from_url(c.get("url", ""))),
                    "price": c.get("inferred_price", 0),
                    "utility": c.get("relevance_score", 0),
                    "our_bid": c.get("bid", 0),
                    "bid_decision": c.get("decision", "pass"),
                    "bid_detail": {"formula": "ceiling × relevance", "utility": c.get("relevance_score", 0), "percentile": None},
                    "url": c.get("url", ""),
                })
                break
        if not bought:
            skipped.append({"sub_query": sq_query, "reason": "no adequate source at or below ceiling"})

    naive_total = 0.0
    for sq, _signals in sq_with_signals:
        top = probe_results.get(sq["query"], {}).get("results", [])
        if top:
            naive_total += min([r.get("inferred_price", 0) for r in top] or [0])

    selected = [c for c in all_candidates if c.get("bid_decision") == "buy"]
    all_scored = sorted(all_candidates, key=lambda x: x.get("utility", 0), reverse=True)
    avg_quality = sum(c.get("utility", 0) for c in selected) / len(selected) if selected else 0
    primary_intent = sq_with_signals[0][1]["intent"] if sq_with_signals else "factual_lookup"
    avg_quality_threshold = (
        sum(s[1].get("quality_threshold", 0.7) for s in sq_with_signals) / max(len(sq_with_signals), 1)
    )
    # Keep legacy rich signal shape for existing UI render logic.
    sigs = extract_signals(query)
    qu = sigs.get("queryUnderstanding") or {}
    qu["query_cluster"] = primary_intent
    qu["tier_strategy"] = "v4_decompose_search_probe_synthesize"
    rules = set(qu.get("routing_rules_fired") or [])
    rules.update(["decompose", "coverage_gap_detection", "valyu_tier_diff_probe"])
    qu["routing_rules_fired"] = list(rules)
    sigs["queryUnderstanding"] = qu
    sigs["intent"] = primary_intent
    sigs["qualityThreshold"] = round(avg_quality_threshold, 3)
    sigs["sub_queries"] = [
        {
            "query": sq["query"],
            "goal": sq.get("goal", ""),
            "research_objective": (sq.get("research_objective") or "").strip(),
            "signals": sig,
        }
        for sq, sig in sq_with_signals
    ]
    sigs["query_fan_out"] = {
        "rationale": query_fan_out_rationale,
        "max_queries": _n_queries,
        "source": (_decomp.get("source") or "local"),
        "planner_framework": "deep_reasoning_researcher",
        "planner_framework_url": DEEP_RESEARCH_FRAMEWORK_URL,
        "refanout_rounds": _refanout_round,
    }
    bid_ceiling = max([sig["max_price_usd"] for _sq, sig in sq_with_signals] or [0.0])

    # Denominator for coverage must match rows we actually ran (search/RAG/probe), not
    # len(sub_queries) from decomposition — those can differ if we hit the deadline early.
    n_subq_processed = len(sq_with_signals)
    n_subq_planned = len(sub_queries)

    sub_query_runs = _build_sub_query_runs(sq_with_signals, free_results, probe_results, search_providers)

    digest = _build_research_digest(sub_query_runs)
    mean_best = (
        sum(r.get("best_coverage", 0) or 0 for r in sub_query_runs) / len(sub_query_runs)
        if sub_query_runs
        else 0.0
    )
    research_completeness = evaluate_research_completeness(
        query,
        digest,
        _instructor_client(),
        {
            "n_subqueries": len(sub_query_runs),
            "subqueries_met_floor": sum(
                1 for r in sub_query_runs if r.get("coverage_status") == "ok"
            ),
            "gap_count": sum(1 for r in sub_query_runs if r.get("coverage_status") == "gap"),
            "mean_best_relevance": mean_best,
        },
    )

    synthesis = _synthesize_research_answer(
        query,
        digest,
        research_completeness,
        len(free_plan),
        len(paid_plan),
        float(total_bid),
        _instructor_client(),
    )

    # Visualizable pipeline log (timestamps are synthetic spacing for readability)
    _tick = [0]
    _plog_base = datetime.now(timezone.utc)
    pipeline_log: list = []

    def _plog(msg: str) -> None:
        ts = (_plog_base + timedelta(seconds=_tick[0])).strftime("%H:%M:%S")
        _tick[0] += 1
        pipeline_log.append({"t": ts, "msg": msg})

    _plog("query received")
    _plog(
        f"fan-out: {n_subq_planned} planned · {n_subq_processed} processed ({(_decomp.get('source') or 'local')})"
    )
    for run in sub_query_runs:
        _plog(
            f"sq{run['index']} signals · intent={run.get('intent') or '?'} · "
            f"domain={run.get('domain') or '?'} · ceiling ${run.get('max_price_usd', 0)}"
        )
        _plog(
            f"sq{run['index']} search ({run['search_provider']}): "
            f"{len(run['free_results'])} results · best relevance {run['best_coverage']:.2f} → {run['coverage_status']}"
        )
        pb = run.get("probe") or {}
        if int(pb.get("paid_count", 0) or 0) > 0 or int(pb.get("free_count", 0) or 0) > 0:
            _plog(
                f"valyu probe sq{run['index']}: free={pb.get('free_count', 0)} "
                f"paid={pb.get('paid_count', 0)} new@paid={pb.get('new_at_paid', 0)}"
            )
    cov_denom = n_subq_processed if n_subq_processed else 0
    cov_str = f"{covered}/{cov_denom}" if cov_denom else f"{covered}/0"
    _plog(
        f"synthesis · coverage {cov_str} · total bid ${round(total_bid, 4)} · "
        f"vs naive {round((1 - total_bid / naive_total) * 100, 1) if naive_total > 0 else 0}% saved"
    )
    rc_mode = research_completeness.get("mode") or "?"
    rc_pct = research_completeness.get("completeness_percent")
    rc_need = research_completeness.get("need_more_information")
    _plog(
        f"critic ({rc_mode}): completeness {rc_pct}% · need_more={rc_need}"
    )
    _plog(f"writer ({synthesis.get('mode', '?')}): {synthesis.get('summary_line', '—')}")

    n_runs = len(sub_query_runs)
    free_ok_n = sum(1 for r in sub_query_runs if r.get("coverage_status") == "ok")
    gap_n = n_runs - free_ok_n

    return {
        "sigs": sigs,
        "selected": selected,
        "ineligible": [],
        "rejected": skipped,
        "allScored": all_scored,
        "bid_ceiling": bid_ceiling,
        "smartCost": round(total_bid, 4),
        "smartQ": round(avg_quality, 4),
        "naiveCost": round(naive_total, 4),
        "naiveQ": 0.0,
        "savings": round(naive_total - total_bid, 4),
        "savingsPct": round((1 - total_bid / naive_total) * 100, 1) if naive_total > 0 else 0,
        "customer_id": customer_id,
        "v4": {
            "free_sources": free_plan,
            "paid_sources": paid_plan,
            "skipped": skipped,
            "coverage": cov_str,
            "subqueries_planned": n_subq_planned,
            "subqueries_processed": n_subq_processed,
            "total_bid": round(total_bid, 4),
            "naive_total": round(naive_total, 4),
            "savings_vs_naive": round((1 - total_bid / naive_total) * 100, 1) if naive_total > 0 else 0,
            "sub_query_runs": sub_query_runs,
            "query_fan_out_rationale": query_fan_out_rationale,
            "query_fan_out_source": (_decomp.get("source") or "local"),
            "pipeline_log": pipeline_log,
            "pipeline_steps": [
                {"id": "decomp", "label": "decomp", "status": "done"},
                {"id": "signals", "label": "signals", "status": "done"},
                {"id": "search", "label": "search", "status": "done"},
                {"id": "probe", "label": "probe", "status": "done"},
                {"id": "plan", "label": "plan", "status": "done"},
                {"id": "writer", "label": "answer", "status": "done"},
            ],
            "synthesis": synthesis,
            "pipeline_stats": {
                "subqueries": n_runs,
                "free_covers": free_ok_n,
                "gaps": gap_n,
            },
            "coverage_policy": COVERAGE_GAP_POLICY,
            "query_fan_out_reference": QUERY_FAN_OUT_REF,
            "research_completeness": research_completeness,
            "deep_research_framework_url": DEEP_RESEARCH_FRAMEWORK_URL,
            "valyu_configured": _valyu_api_key_present(),
        },
    }


# ═══════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/admin")
def admin():
    """Admin dashboard: learning system and persistence (internal use)."""
    return send_from_directory(".", "admin.html")


@app.route("/api-reference")
def api_reference():
    """Interactive API documentation for content routing, purchase plan, and bidding."""
    return send_from_directory(".", "api-reference.html")


@app.route("/optimize", methods=["POST"])
def optimize_route():
    data = request.get_json() or {}
    query = data.get("query", "")
    customer_id = data.get("customer_id", "default")
    try:
        result = optimize(query, customer_id=customer_id)
    except Exception as e:
        app.logger.exception("optimize failed")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
            ),
            500,
        )

    # Articles to scrape: show every search result (no filter by purchase plan).
    # Each result is turned into an article; catalog domains get name+price, others get domain label + "—".
    # COMMENTED OUT: search integration
    # result["search_configured"] = is_search_configured()
    # result["search_provider"] = get_search_provider_name()
    # result["selected_articles"] = []
    # if is_search_configured():
    #     try:
    #         search_results, provider_used = fetch_search_results(query, num=15)
    #         result["selected_articles"] = _search_results_to_articles(search_results)
    #         result["search_provider"] = provider_used
    #         if not result["selected_articles"] and search_results:
    #             app.logger.warning(
    #                 "Search returned %s results but 0 articles (query %r). First result keys: %s",
    #                 len(search_results), query[:40], list(search_results[0].keys()) if search_results else None,
    #             )
    #         elif not result["selected_articles"]:
    #             app.logger.info(
    #                 "Search returned 0 results for %r (provider %s). Tip: set DSAIL_BRAVE_API_KEY (or BRAVE_API_KEY) in .env for reliable search.",
    #                 query[:40], provider_used,
    #             )
    #     except Exception as e:
    #         app.logger.warning("Search failed for %r: %s", query[:50], e)
    result["search_configured"] = False
    result["search_provider"] = None
    result["selected_articles"] = []

    # Persist conversion event for learning (purchase decision; outcomes via /feedback)
    event_id = str(uuid.uuid4())
    query_id = str(uuid.uuid4())
    selected_names = [s["name"] for s in result["selected"]]
    avg_confidence = sum(s.get("utility", 0) for s in result["selected"]) / len(result["selected"]) if result["selected"] else 0
    qu = result["sigs"].get("queryUnderstanding") or {}
    event = ConversionEvent(
        event_id=event_id,
        query_id=query_id,
        customer_id=customer_id,
        query_text=query,
        query_cluster=qu.get("query_cluster") or result["sigs"]["intent"],
        intent=result["sigs"]["intent"],
        sources_purchased=selected_names,
        total_cost=result["smartCost"],
        decision_confidence=round(avg_confidence, 4),
    )
    get_metrics_store().log_event(event)
    result["event_id"] = event_id
    result["query_id"] = query_id

    return jsonify(result)


@app.route("/feedback", methods=["POST"])
def feedback_route():
    """Submit outcome feedback for a prior optimization (sources cited, quality, correction)."""
    data = request.get_json() or {}
    event_id = data.get("event_id")
    if not event_id:
        return jsonify({"ok": False, "error": "event_id required"}), 400
    sources_cited = data.get("sources_cited", [])
    answer_quality = data.get("answer_quality")
    user_rating = data.get("user_rating")
    correction_made = data.get("correction_made", False)
    ok = get_metrics_store().submit_feedback(
        event_id=event_id,
        sources_cited=sources_cited,
        answer_quality=answer_quality,
        user_rating=user_rating,
        correction_made=correction_made,
    )
    if not ok:
        return jsonify({"ok": False, "error": "event_id not found"}), 404
    return jsonify({"ok": True})


@app.route("/learn", methods=["GET"])
def learn_route():
    """Return learned publisher performance by query cluster (k-anonymity applied)."""
    cluster = request.args.get("cluster")
    min_sample = request.args.get("min_sample_size", type=int) or 5
    payload = get_metrics_store().get_global_publisher_performance(
        query_cluster=cluster or None,
        min_sample_size=min_sample,
    )
    payload["event_count"] = get_metrics_store().event_count()
    return jsonify(payload)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5001, help="Port (default 5001; macOS often uses 5000 for AirPlay)")
    p.add_argument("--host", default="127.0.0.1", help="Bind host")
    args = p.parse_args()
    _get_spacy_nlp()
    print(f" * Open in browser: http://{args.host}:{args.port}/")
    app.run(debug=True, host=args.host, port=args.port)
