# LM Tree: Technical Implementation & Integration Guide

## How the prototype works

### Data flow

```
build_demo_corpus()          33 synthetic ContentItems (text, url, y=0/1)
       │
       ▼
grow_node(root)
  ├─ explore_arms()          Try 5 bid multiplier arms [0.2, 0.4, 0.6, 0.8, 1.0]
  │                          Each arm k: value_k = arm_k × fraction_of_items_that_convert_at_k
  │                          Pick arm with highest value_k → node.optimal_arm
  │
  ├─ Partition H_n / L_n     H_n = items with y=1 that converted at top-half arms (≥ 0.6)
  │                          L_n = items with y=0 or converted only at bottom-half arms
  │
  ├─ discover_feature()      Claude reads 5 samples from each set, returns JSON:
  │                          {"type": "existence", "description": "...", "keywords": [...]}
  │
  ├─ annotate_items()        Keyword-match every item against the discovered rule → bool
  │
  ├─ Validate split          Re-run explore_arms on each child subset
  │                          If |left_arm - right_arm| < 0.15 → discard, make leaf
  │
  └─ Recurse                 grow_node(left_child), grow_node(right_child)
```

### Key design decision: inference without LLM

At training time, Claude identifies a split rule and returns `keywords: [...]`. At inference time, `predict()` only uses those keywords — no API call. This means:

- Training: O(depth × 2 Claude calls per node) — done once, offline
- Inference: O(depth) string comparisons — runs inline in the bid pipeline

### What `y` means

`y = 1` means "this content was worth buying." In the demo this is simulated. In production it maps to `sources_cited` in `ConversionEvent` — if a URL appears in `sources_cited`, that's a positive training example for the content at that URL.

---

## Current bid pipeline (what needs to change)

The bid for a content item is computed in two steps:

**Step 1 — Ceiling** ([app.py:746](app.py))
```python
def compute_bid_ceiling(sigs: dict, best_free: float) -> float:
    base = INTENT_BASE.get(sigs["intent"], 0.25)   # e.g. "financial_analysis" → $0.50
    multiplier = 0.6 + sigs["complexity_score"] * 0.4
    # gap scaling, domain boosts...
    return base * multiplier    # e.g. $0.35
```
This is per-query, not per-article. It doesn't look at article text at all.

**Step 2 — Relevance score** ([app.py:1544](app.py))
```python
def _relevance_score(result: dict, signals: dict) -> float:
    entity_cov   = ...  # 35% weight: do query entities appear in title/snippet?
    keyword_score = ... # 25% weight: do query keywords appear?
    type_score   = ...  # 25% weight: does source match content_type_needed?
    temporal     = ...  # 15% weight: is there a date if freshness required?
    return entity_cov * 0.35 + keyword_score * 0.25 + type_score * 0.25 + temporal * 0.15
```
This produces a 0–1 score per article, purely based on query-article overlap. It has no memory of which articles historically got cited.

**Step 3 — Bid** ([app.py:1561](app.py))
```python
def _compute_bid(signals: dict, result: dict) -> dict:
    rel = _relevance_score(result, signals)
    bid = signals["max_price_usd"] * rel    # ceiling × relevance
    decision = "buy" if bid >= result["inferred_price"] else "pass"
```

---

## What to change to integrate the LM Tree

### Option A — Replace `_relevance_score()` entirely (full integration)

The LM Tree's `predict()` returns a multiplier in [0.2, 1.0]. Swap it in directly:

```python
# In app.py, after importing lm_tree:
from lm_tree import predict, load_tree

_LM_TREE = load_tree("lm_tree_model.json")  # serialized tree, trained offline

def _compute_bid(signals: dict, result: dict) -> dict:
    text = f"{result.get('title', '')} {result.get('snippet', '')}"
    
    if _LM_TREE is not None:
        multiplier, path = predict(_LM_TREE, text)
    else:
        multiplier = _relevance_score(result, signals)   # fallback
    
    bid = round(signals["max_price_usd"] * multiplier, 4)
    inferred = result.get("inferred_price", 0)
    return {
        "relevance_score": multiplier,
        "bid": bid,
        "inferred_price": inferred,
        "decision": "buy" if bid >= inferred else "pass",
        "reasoning": f"ceiling ${signals['max_price_usd']} × lm_tree {multiplier:.2f} = bid ${bid:.4f}",
    }
```

Two functions to add to `lm_tree.py`:
- `save_tree(node, path)` — serialize tree to JSON
- `load_tree(path) -> TreeNode | None` — deserialize, return None if file missing

### Option B — Use LM Tree as a signal alongside the existing score (safer)

Keep `_relevance_score()` and blend:

```python
def _compute_bid(signals: dict, result: dict) -> dict:
    text = f"{result.get('title', '')} {result.get('snippet', '')}"
    rel = _relevance_score(result, signals)

    if _LM_TREE is not None:
        lm_multiplier, _ = predict(_LM_TREE, text)
        combined = 0.5 * rel + 0.5 * lm_multiplier   # blend 50/50
    else:
        combined = rel

    bid = round(signals["max_price_usd"] * combined, 4)
    ...
```

This is lower risk — if the tree produces bad multipliers, the static score acts as a floor.

---

## How to train on real data (not synthetic)

The prototype uses fake `y` values. Production training uses `learning.db`.

```python
# Pseudocode: build_real_corpus() to replace build_demo_corpus()

import sqlite3
from lm_tree import ContentItem

def build_real_corpus(db_path="learning.db") -> list[ContentItem]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT query_text, sources_purchased, sources_cited
        FROM conversion_events
        WHERE sources_purchased != '[]'
    """).fetchall()

    items = []
    for query_text, purchased_json, cited_json in rows:
        purchased = json.loads(purchased_json)
        cited = set(json.loads(cited_json))
        for url in purchased:
            # y=1 if this URL was cited, 0 if purchased but not cited
            y = 1.0 if url in cited else 0.0
            # You need the article text — store snippet at purchase time
            # (see "what to log" section below)
            text = lookup_snippet(url)
            items.append(ContentItem(id=url, text=text, url=url, y=y))
    return items
```

**What you need to log that isn't logged yet:**

`ConversionEvent` stores URLs (`sources_purchased`, `sources_cited`) but not the article text. To train on real data you need to also store the title + snippet at purchase time. Add a field to `ConversionEvent` in [learning.py:24](learning.py):

```python
@dataclass
class ConversionEvent:
    ...
    # Add this field:
    source_snippets: Dict[str, str] = field(default_factory=dict)
    # Maps url → "title. snippet" — the text the tree trains on
```

And populate it in `_compute_bid()` when a buy decision is made:

```python
# In _run_fanout_round or wherever the ConversionEvent is created:
event.source_snippets[url] = f"{result['title']}. {result['snippet']}"
```

---

## Retraining cadence

The tree is trained offline and loaded as a static file. A simple setup:

```
lm_tree_model.json    ← loaded by app.py at startup, never changes at runtime
lm_tree.py train      ← run manually (or on a cron) to rebuild from learning.db
```

When you have enough data (50+ cited/not-cited pairs), retrain:

```bash
python lm_tree.py --train-real   # reads learning.db, writes lm_tree_model.json
```

Add a `--train-real` flag to `lm_tree.py` that calls `build_real_corpus()` instead of `build_demo_corpus()`.

---

## What "enough data" means

The tree needs a minimum number of items per node to attempt a split (`MIN_SPLIT_SIZE = 3`). Practically:

- **To get any tree at all:** ~20 events with citation outcomes
- **To get depth-2 splits:** ~50+ events
- **To get stable, trustworthy splits:** ~200+ events per intent cluster

Until you have real data, the keyword fallback in `discover_feature()` acts as a reasonable prior.

---

## Summary of files to touch

| File | Change | Why |
|---|---|---|
| [lm_tree.py](lm_tree.py) | Add `save_tree()`, `load_tree()`, `--train-real` flag | Persistence + real data training |
| [learning.py:44](learning.py) | Add `source_snippets: Dict[str, str]` to `ConversionEvent` | Store text needed for training |
| [app.py:1561](app.py) | Replace/blend `_relevance_score()` in `_compute_bid()` | Use tree multiplier in live bids |
| [app.py:startup] | `_LM_TREE = load_tree("lm_tree_model.json")` | Load tree once at boot |
