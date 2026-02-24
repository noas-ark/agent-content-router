import math
import re
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════

SOURCES = [
    {"name": "Bloomberg",       "price": 3.00, "auth": .95, "topics": ["finance","economics","markets"],   "freshH": 2,  "type": "premium",
     "priceSource": "Cloudflare Pay-Per-Crawl",    "priceDetail": "Bloomberg registered with Cloudflare's pay-per-crawl program. 402 response header returns crawler-price: 3.00 USD. Premium financial content, single-article access."},
    {"name": "WSJ",             "price": 2.50, "auth": .93, "topics": ["finance","business","politics"],   "freshH": 4,  "type": "premium",
     "priceSource": "TollBit registered publisher", "priceDetail": "WSJ is listed in TollBit's publisher catalog at $2.50/article. Pricing verified against TollBit's public rate card. WSJ also has a Microsoft PCM deal but per-article access is TollBit-routed."},
    {"name": "Financial Times", "price": 3.50, "auth": .94, "topics": ["finance","geopolitics","trade"],   "freshH": 3,  "type": "premium",
     "priceSource": "RSL license + TollBit",        "priceDetail": "FT publishes RSL terms at ft.com/robots.txt pointing to rsl-license.xml. Pay-per-crawl rate set at $3.50, classified as premium analysis. TollBit acts as merchant of record."},
    {"name": "Reuters",         "price": 0.80, "auth": .88, "topics": ["news","finance","breaking"],       "freshH": 1,  "type": "wire",
     "priceSource": "TollBit wire tier",            "priceDetail": "Reuters wire content is priced at the budget tier on TollBit — high volume, fast-turnover news. 402 response includes crawler-price: 0.80. Lower price reflects commodity wire distribution model."},
    {"name": "AP",              "price": 0.70, "auth": .87, "topics": ["news","general","breaking"],       "freshH": 1,  "type": "wire",
     "priceSource": "Cloudflare Pay-Per-Crawl",    "priceDetail": "AP uses Cloudflare's AI Crawl Control. 402 header: crawler-price: 0.70 USD. Slightly cheaper than Reuters; AP distributes syndicated wire broadly and prices for volume AI access."},
    {"name": "NYT",             "price": 1.50, "auth": .91, "topics": ["news","politics","culture"],       "freshH": 6,  "type": "mid",
     "priceSource": "Microsoft PCM",               "priceDetail": "NYT is a launch partner in Microsoft's Publisher Content Marketplace (PCM). Usage-based pricing at ~$1.50/article for AI assistant access. PCM handles identity verification (KYA) and settlement via Stripe."},
    {"name": "TechCrunch",      "price": 0.50, "auth": .82, "topics": ["tech","startups","AI"],            "freshH": 3,  "type": "mid",
     "priceSource": "TollBit mid-tier",            "priceDetail": "TechCrunch is in TollBit's standard publisher catalog. Mid-tier price at $0.50. Content is high-volume, topically specific (tech). 402 response negotiated via TollBit's bot authentication layer."},
    {"name": "Brookings",       "price": 0.00, "auth": .89, "topics": ["policy","research","economics"],   "freshH": 72, "type": "free",
     "priceSource": "Open access / no paywall",    "priceDetail": "Brookings Institution publishes all content under open access. No robots.txt restriction on AI crawling. No RSL license required. Free to access — but no freshness guarantee and no 402 flow."},
    {"name": "arXiv",           "price": 0.00, "auth": .87, "topics": ["science","AI","engineering"],      "freshH": 24, "type": "free",
     "priceSource": "Open access (Cornell)",       "priceDetail": "arXiv is operated by Cornell University with a fully open-access mandate. All preprints are freely crawlable. No TollBit, no 402, no RSL. High authority for technical queries but no editorial curation or breaking news."},
    {"name": "Wikipedia",       "price": 0.00, "auth": .75, "topics": ["general","reference","history"],   "freshH": 168,"type": "free",
     "priceSource": "CC BY-SA license",            "priceDetail": "Wikipedia content is licensed under Creative Commons Attribution-ShareAlike. Freely crawlable and trainable with attribution. No paywall, no 402 response. Lowest freshness of all sources (weekly update cycle)."},
]

DOMAIN_BOOST = {
    "financial_analysis": {"Bloomberg": .32, "WSJ": .24, "Financial Times": .28, "Reuters": .12},
    "breaking_news":      {"Reuters": .30, "AP": .27, "Bloomberg": .14, "NYT": .10},
    "tech_product":       {"TechCrunch": .32, "arXiv": .14},
    "explainer":          {"Wikipedia": .22, "Brookings": .17, "arXiv": .20},
    "policy":             {"Brookings": .32, "NYT": .15, "Financial Times": .14},
    "medical_clinical":   {"arXiv": .30, "NYT": .12},
}

REDUNDANT = [["Reuters", "AP"], ["Bloomberg", "Reuters"]]

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

    return {
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


def score_source(sigs, src):
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

    boost = DOMAIN_BOOST.get(intent, {}).get(src["name"], 0)
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


def optimize(query):
    sigs   = extract_signals(query)
    budget = 12.0

    scored = [{**s, **score_source(sigs, s)} for s in SOURCES]

    # GATE 1: Eligibility (hard filters)
    eligible, ineligible = [], []
    for s in scored:
        reasons = []
        if s["freshH"] > sigs["maxFreshnessHours"]:
            reasons.append("too_stale")
        if s["utility"] < sigs["qualityThreshold"] - 0.12:
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
        "smartCost":  spent,
        "smartQ":     smart_q,
        "naiveCost":  naive_cost,
        "naiveQ":     naive_q,
        "savings":    naive_cost - spent,
        "savingsPct": (naive_cost - spent) / naive_cost * 100 if naive_cost > 0 else 0,
    }


# ═══════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/optimize", methods=["POST"])
def optimize_route():
    data  = request.get_json()
    query = data.get("query", "")
    result = optimize(query)
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True)
