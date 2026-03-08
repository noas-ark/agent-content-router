import math
import random
import re
import uuid
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, send_from_directory

from learning import ConversionEvent, get_metrics_store
from search_provider import fetch_search_results, is_search_configured, get_search_provider_name

app = Flask(__name__)

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
            "title": (r.get("title") or "").strip() or "(No title)",
            "url": link,
            "source_name": source_name,
            "price": price,
            "snippet": (r.get("snippet") or "").strip(),
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


def extract_signals(query):
    q = query.lower()
    words = q.split()

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
    has_entity = bool(re.search(r"\b[A-Z][a-z]{2,}\b", query))
    what_happened = bool(re.search(r"\bwhat('s|\s+is|\s+happened|\s+happening)\b", q))
    # Override: (1) "what happened/happening" implies news even with lowercase "iran"; (2) ambiguous + entity
    if what_happened or (top_intent_score < 0.12 and has_entity):
        intent = "breaking_news"
        top_intent_score = 0.5
    semantic_raw = min(top_intent_score * 3.8 + 0.22, 0.98)

    # ── DIMENSION 1: RELEVANCE ────────────────────────────────
    entity_re = r'\b([A-Z][a-z]{1,}(?:\s[A-Z][a-z]{1,})*|[A-Z]{2,6})\b'
    raw_entities = re.findall(entity_re, query)
    skip = {'The', 'A', 'An', 'In', 'On', 'At', 'Is', 'It', 'If', 'Do', 'Be', 'We', 'My'}
    entities = [e for e in raw_entities if len(e) > 1 and e not in skip]
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


def compute_bid_ceiling(sigs: dict) -> float:
    """
    Per-query value ceiling (max bid) based on scoring signals.
    Higher stakes/credibility/freshness → higher ceiling (bootk.ai DSP logic).
    """
    cred = sigs.get("credibility") or {}
    fresh = sigs.get("freshness") or {}
    stakes = cred.get("stakes", 0.4) if isinstance(cred, dict) else 0.4
    cred_v = cred.get("composed", 0.5) if isinstance(cred, dict) else 0.5
    fresh_v = fresh.get("composed", 0.5) if isinstance(fresh, dict) else 0.5
    base = 0.002  # $0.002 minimum
    cap = 0.008   # $0.008 max for high-stakes
    factor = 0.4 * stakes + 0.3 * cred_v + 0.3 * fresh_v
    return round(base + (cap - base) * factor, 4)


def optimize(query, customer_id="default"):
    sigs   = extract_signals(query)
    budget = 12.0
    bid_ceiling = compute_bid_ceiling(sigs)

    # Learned publisher performance for this intent (citation rate / value per dollar)
    store = get_metrics_store()
    learned_boost = store.get_learned_domain_boost(sigs["intent"])

    scored = [{**s, **score_source(sigs, s, learned_boost)} for s in SOURCES]

    # Add bidding fields: bid closer to ask (realistic); our_bid = ask × (0.72 + 0.28 × utility)
    for s in scored:
        if s["price"] == 0:
            s["our_bid"] = 0
            s["bid_decision"] = "buy"
            s["bid_detail"] = {"formula": "FREE", "utility": s["utility"], "others": [], "percentile": None}
        else:
            coef = 0.72 + 0.28 * s["utility"]
            s["our_bid"] = round(s["price"] * coef, 3)
            s["bid_decision"] = "buy" if s["our_bid"] >= s["price"] else "pass"
            # Simulate anonymized other bidders (3–6 bids around our_bid)
            n_others = random.randint(3, 6)
            others = [round(s["our_bid"] * (0.75 + 0.5 * random.random()), 3) for _ in range(n_others)]
            others.sort()
            median_other = others[len(others) // 2] if others else s["our_bid"]
            rank = sum(1 for o in others if o < s["our_bid"])
            pct = round(100 * rank / max(len(others), 1))
            s["bid_detail"] = {
                "formula": f"Ask × (0.72 + 0.28 × utility)",
                "utility": round(s["utility"], 3),
                "utility_pct": round(100 * s["utility"]),
                "n_others": n_others,
                "median_other": median_other,
                "percentile": pct,
            }

    # GATE 1: Eligibility (hard filters)
    eligible, ineligible = [], []
    intent = sigs["intent"]
    for s in scored:
        reasons = []
        if s["freshH"] > sigs["maxFreshnessHours"]:
            reasons.append("too_stale")
        if s["utility"] < sigs["qualityThreshold"] - 0.12:
            reasons.append("low_utility")
        # breaking_news: require topic overlap with news/current events (exclude tech/academic-only)
        if intent == "breaking_news" and s.get("semantic", 0) < 0.35:
            reasons.append("low_utility")
        if reasons:
            ineligible.append({**s, "reason": reasons[0]})
        else:
            eligible.append(s)

    # GATE 2: Value rank among eligible
    eligible.sort(key=lambda s: s["utility"] / max(s["price"], 0.01), reverse=True)

    # GATE 3: Select with diversity
    selected, rejected = [], []
    spent      = 0.0
    used_types = set()
    used_names = set()

    for c in eligible:
        if spent + c["price"] > budget:
            rejected.append({**c, "reason": "over_budget"})
            continue
        redundant = any(
            (c["name"] == a and b in used_names) or (c["name"] == b and a in used_names)
            for a, b in REDUNDANT
        )
        if redundant:
            rejected.append({**c, "reason": "redundant"})
            continue
        dup_type = c["type"] != "free" and c["type"] in used_types and len(selected) >= sigs["minSources"]
        if dup_type:
            rejected.append({**c, "reason": "dup_tier"})
            continue
        selected.append(c)
        spent += c["price"]
        used_types.add(c["type"])
        used_names.add(c["name"])
        if len(selected) >= max(sigs["minSources"] + 1, 2):
            break

    naive      = sorted(SOURCES, key=lambda s: -s["auth"])[:3]
    naive_cost = sum(s["price"] for s in naive)
    naive_q    = sum(s["auth"] for s in naive) / len(naive)
    smart_q    = sum(s["utility"] for s in selected) / len(selected) if selected else 0

    return {
        "sigs":       sigs,
        "selected":   selected,
        "ineligible": ineligible[:6],
        "rejected":   rejected[:4],
        "allScored":  scored,
        "bid_ceiling": bid_ceiling,
        "smartCost":  spent,
        "smartQ":     smart_q,
        "naiveCost":  naive_cost,
        "naiveQ":     naive_q,
        "savings":    naive_cost - spent,
        "savingsPct": (naive_cost - spent) / naive_cost * 100 if naive_cost > 0 else 0,
        "customer_id": customer_id,
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
    result = optimize(query, customer_id=customer_id)

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
    #                 "Search returned 0 results for %r (provider %s). Tip: set BRAVE_API_KEY in .env for reliable search.",
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
    print(f" * Open in browser: http://{args.host}:{args.port}/")
    app.run(debug=True, host=args.host, port=args.port)
