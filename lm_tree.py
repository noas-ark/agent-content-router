"""
LM Tree — Buyer-Side WTP Demo
==============================
Adapts the LM Tree algorithm (arxiv.org/abs/2604.01416) to the buyer side:
instead of "what price should a seller charge?" we ask "what bid multiplier
should bootk.ai apply to this content?"

Training signal: citation/quality outcome (y=1 = content was cited after purchase).
The tree discovers textual features that predict high citation → high bid multiplier.

Run:
    python lm_tree.py                   # uses mock LLM (no key needed)
    ANTHROPIC_API_KEY=sk-... python lm_tree.py   # uses real Claude
"""

import json
import math
import os
import random
import re
import textwrap
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(override=True)

# ── Config ────────────────────────────────────────────────────────────────────

ARMS = [0.2, 0.4, 0.6, 0.8, 1.0]          # bid multiplier arms
MIN_SPLIT_SIZE = 3                          # min items in each contrast set to attempt a split
SPLIT_DELTA_THRESHOLD = 0.15               # min arm difference to keep a split
MAX_DEPTH = 3
RANDOM_SEED = 42

random.seed(RANDOM_SEED)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ContentItem:
    id: str
    text: str          # title + snippet — what we have at bid time
    url: str
    y: float           # citation outcome 0 or 1 (training signal)
    # assigned during arm exploration — which arm "converted" this item
    assigned_arm: float = 0.0


@dataclass
class SplitRule:
    type: str                  # "existence"
    description: str           # human-readable label shown in viz
    keywords: list[str]        # fast keyword match for inference (no LLM needed)


@dataclass
class ArmStat:
    arm: float
    citation_rate: float
    value: float               # arm × citation_rate


@dataclass
class TreeNode:
    node_id: str
    depth: int
    items: list[ContentItem]
    optimal_arm: float = 0.0
    arm_stats: list[ArmStat] = field(default_factory=list)
    split_rule: Optional[SplitRule] = None
    left: Optional["TreeNode"] = None   # matches split rule
    right: Optional["TreeNode"] = None  # does not match


# ── Arm exploration (multi-armed bandit step) ─────────────────────────────────

def explore_arms(items: list[ContentItem]) -> tuple[float, list[ArmStat]]:
    """
    For each arm k, simulate conversion: item converts if y=1 AND its simulated
    WTP (drawn once per item) is >= arm. Returns (optimal_arm, arm_stats).
    """
    # Draw a stable WTP for each item (seeded by item id for reproducibility)
    wtp = {}
    for item in items:
        rng = random.Random(item.id)
        # High-y items cluster near 1.0; low-y items near 0.2
        if item.y >= 0.5:
            wtp[item.id] = rng.gauss(0.85, 0.15)
        else:
            wtp[item.id] = rng.gauss(0.3, 0.15)
        wtp[item.id] = max(0.05, min(1.0, wtp[item.id]))

    stats = []
    for arm in ARMS:
        converts = [it for it in items if wtp[it.id] >= arm]
        citation_rate = len(converts) / len(items) if items else 0.0
        value = arm * citation_rate
        stats.append(ArmStat(arm=arm, citation_rate=citation_rate, value=value))

    best = max(stats, key=lambda s: s.value)

    # Tag each item with the arm at which it "converted" (for H_n / L_n partition)
    midpoint = ARMS[len(ARMS) // 2]
    for item in items:
        # Assign to the highest arm that item would convert at
        item.assigned_arm = max(
            (a for a in ARMS if wtp[item.id] >= a),
            default=0.0
        )

    return best.arm, stats


# ── LLM feature discovery (with deterministic fallback) ──────────────────────

def _call_claude(prompt: str, system: str = "") -> str:
    """Call Claude API; returns empty string if unavailable."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msgs = [{"role": "user", "content": prompt}]
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system or "You are a pricing analyst. Be concise and structured.",
            messages=msgs,
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"  [Claude API error: {e}]")
        return ""


_PROPRIETARY_KWS = [
    "proprietary", "exclusive", "primary research", "first-party",
    "earnings", "forecast", "survey data", "internal", "benchmark",
    "clinical trial", "patent", "licensed", "embargoed", "pre-release",
]
_STATS_KWS = [
    "percent", "%", "basis points", "bps", "data series", "dataset",
    "quarterly", "annual report", "figures", "statistics", "metric",
    "index", "yield", "margin", "revenue", "eps", "ebitda",
]


def _keyword_match(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(kw.lower() in t for kw in keywords)


def discover_feature(
    h_items: list[ContentItem],
    l_items: list[ContentItem],
    depth: int,
) -> Optional[SplitRule]:
    """
    LLM Analyst step: identify one textual feature distinguishing H_n from L_n.
    Falls back to keyword heuristics when no API key is available.
    """
    h_texts = "\n".join(f"- {it.text[:200]}" for it in h_items[:5])
    l_texts = "\n".join(f"- {it.text[:200]}" for it in l_items[:5])

    prompt = textwrap.dedent(f"""
        You are analyzing content items to find pricing-relevant attributes.

        HIGH-VALUE items (frequently cited by AI researchers after purchase):
        {h_texts}

        LOW-VALUE items (rarely cited):
        {l_texts}

        Identify ONE textual attribute that best separates the high-value from
        low-value items. Output ONLY valid JSON (no markdown fences), like:
        {{"type": "existence", "description": "mentions proprietary data or exclusive research", "keywords": ["proprietary", "exclusive", "primary research"]}}
    """).strip()

    raw = _call_claude(prompt)

    # Try to parse Claude's response
    if raw:
        try:
            # strip any markdown code fences Claude might add
            raw_clean = re.sub(r"```[a-z]*\n?", "", raw).strip("` \n")
            obj = json.loads(raw_clean)
            if all(k in obj for k in ("type", "description", "keywords")):
                return SplitRule(
                    type=obj["type"],
                    description=obj["description"],
                    keywords=obj["keywords"],
                )
        except Exception:
            pass

    # ── Deterministic fallback (no API key needed) ────────────────────────────
    # Depth 0: check for proprietary/exclusive signals
    # Depth 1: check for quantitative data signals
    # Depth 2: check for recency/primary-source signals

    if depth == 0:
        # Does H_n have more proprietary keywords than L_n?
        h_hits = sum(1 for it in h_items if _keyword_match(it.text, _PROPRIETARY_KWS))
        l_hits = sum(1 for it in l_items if _keyword_match(it.text, _PROPRIETARY_KWS))
        if h_hits > l_hits:
            return SplitRule(
                type="existence",
                description="mentions proprietary data, exclusive research, or primary sourcing",
                keywords=_PROPRIETARY_KWS,
            )

    if depth == 1:
        h_hits = sum(1 for it in h_items if _keyword_match(it.text, _STATS_KWS))
        l_hits = sum(1 for it in l_items if _keyword_match(it.text, _STATS_KWS))
        if h_hits > l_hits:
            return SplitRule(
                type="existence",
                description="includes numeric statistics, quantified results, or financial metrics",
                keywords=_STATS_KWS,
            )

    recency_kws = ["2024", "2025", "2026", "q1", "q2", "q3", "q4", "latest", "breaking", "flash"]
    h_hits = sum(1 for it in h_items if _keyword_match(it.text, recency_kws))
    l_hits = sum(1 for it in l_items if _keyword_match(it.text, recency_kws))
    if h_hits > l_hits:
        return SplitRule(
            type="existence",
            description="contains recent or time-sensitive data (2024–2026, quarterly updates)",
            keywords=recency_kws,
        )

    return None


def annotate_items(
    items: list[ContentItem],
    rule: SplitRule,
) -> dict[str, bool]:
    """
    LLM Annotator step: label each item as matching/not-matching the rule.
    Uses keyword matching directly (no LLM needed at inference time).
    """
    return {it.id: _keyword_match(it.text, rule.keywords) for it in items}


# ── Tree growth ───────────────────────────────────────────────────────────────

def grow_node(node: TreeNode, max_depth: int) -> TreeNode:
    """Recursively grow the tree from this node."""
    prefix = "  " * node.depth

    # Step 1: arm exploration
    opt_arm, stats = explore_arms(node.items)
    node.optimal_arm = opt_arm
    node.arm_stats = stats

    print(f"{prefix}Node {node.node_id} ({len(node.items)} items) → optimal arm: {opt_arm:.1f}")

    if node.depth >= max_depth:
        print(f"{prefix}  Max depth reached → LEAF")
        return node

    # Step 2: partition into H_n (high-WTP) and L_n (low-WTP)
    midpoint = ARMS[len(ARMS) // 2]
    h_items = [it for it in node.items if it.y >= 0.5 and it.assigned_arm >= midpoint]
    l_items = [it for it in node.items if it.y < 0.5 or it.assigned_arm < midpoint]

    if min(len(h_items), len(l_items)) < MIN_SPLIT_SIZE:
        print(f"{prefix}  Contrast sets too small (H={len(h_items)}, L={len(l_items)}) → LEAF")
        return node

    # Step 3: discover feature
    print(f"{prefix}  Discovering split feature (H={len(h_items)}, L={len(l_items)})...")
    rule = discover_feature(h_items, l_items, node.depth)

    if rule is None:
        print(f"{prefix}  No feature found → LEAF")
        return node

    print(f"{prefix}  Rule: \"{rule.description}\"")

    # Step 4: annotate all items
    labels = annotate_items(node.items, rule)
    left_items = [it for it in node.items if labels[it.id]]
    right_items = [it for it in node.items if not labels[it.id]]

    if min(len(left_items), len(right_items)) < MIN_SPLIT_SIZE:
        print(f"{prefix}  Split too skewed (L={len(left_items)}, R={len(right_items)}) → LEAF")
        return node

    # Step 5: validate — do children converge to different arms?
    left_arm, _ = explore_arms(left_items)
    right_arm, _ = explore_arms(right_items)
    delta = abs(left_arm - right_arm)

    if delta < SPLIT_DELTA_THRESHOLD:
        print(f"{prefix}  Child arms too similar ({left_arm:.1f} vs {right_arm:.1f}, Δ={delta:.2f}) → LEAF")
        return node

    print(f"{prefix}  Split valid! L-arm={left_arm:.1f}, R-arm={right_arm:.1f}, Δ={delta:.2f}")
    node.split_rule = rule

    # Step 6: recurse
    node.left = grow_node(
        TreeNode(node_id=node.node_id + "L", depth=node.depth + 1, items=left_items),
        max_depth,
    )
    node.right = grow_node(
        TreeNode(node_id=node.node_id + "R", depth=node.depth + 1, items=right_items),
        max_depth,
    )

    return node


# ── Inference ─────────────────────────────────────────────────────────────────

def predict(node: TreeNode, content_text: str) -> tuple[float, list[str]]:
    """
    Traverse the tree using keyword matching only (no LLM at inference time).
    Returns (bid_multiplier, path_taken).
    """
    path = [f"ROOT (arm={node.optimal_arm:.1f})"]

    current = node
    while current.split_rule is not None and current.left and current.right:
        matched = _keyword_match(content_text, current.split_rule.keywords)
        if matched:
            path.append(f"YES → \"{current.split_rule.description}\"")
            current = current.left
        else:
            path.append(f"NO  → not \"{current.split_rule.description}\"")
            current = current.right
        path.append(f"Node {current.node_id} (arm={current.optimal_arm:.1f})")

    return current.optimal_arm, path


# ── Pretty-print ──────────────────────────────────────────────────────────────

def print_tree(node: TreeNode, indent: int = 0) -> None:
    pad = "  " * indent
    is_leaf = node.split_rule is None or node.left is None
    tag = "LEAF" if is_leaf else "SPLIT"
    rule_str = f"  ┤ \"{node.split_rule.description}\"" if node.split_rule else ""
    print(f"{pad}[{tag}] {node.node_id}: arm={node.optimal_arm:.1f}  n={len(node.items)}{rule_str}")
    if node.left:
        print(f"{pad}  ├─ YES →")
        print_tree(node.left, indent + 2)
    if node.right:
        print(f"{pad}  └─ NO  →")
        print_tree(node.right, indent + 2)


# ── D3.js Visualization ───────────────────────────────────────────────────────

def tree_to_json(node: TreeNode) -> dict:
    """Serialize TreeNode to D3-compatible hierarchy dict."""
    is_leaf = node.split_rule is None or node.left is None
    data: dict = {
        "id": node.node_id,
        "depth": node.depth,
        "n_items": len(node.items),
        "optimal_arm": node.optimal_arm,
        "is_leaf": is_leaf,
        "split_rule": node.split_rule.description if node.split_rule else None,
        "arm_stats": [
            {"arm": s.arm, "citation_rate": round(s.citation_rate, 3), "value": round(s.value, 3)}
            for s in node.arm_stats
        ],
    }
    if node.left or node.right:
        data["children"] = []
        if node.left:
            child = tree_to_json(node.left)
            child["edge_label"] = "YES"
            data["children"].append(child)
        if node.right:
            child = tree_to_json(node.right)
            child["edge_label"] = "NO"
            data["children"].append(child)
    return data


def generate_viz(root: TreeNode, out_path: str = "lm_tree_viz.html") -> str:
    tree_data = json.dumps(tree_to_json(root), indent=2)

    # Also emit sample content items for the prediction playground
    sample_items = [
        {"text": "Goldman Sachs: Proprietary semiconductor demand forecast Q2 2025 — exclusive 47-series dataset", "label": "High-value: proprietary + quantified"},
        {"text": "Morgan Stanley: Exclusive survey of 300 CIOs, primary research, AI infrastructure spend", "label": "High-value: primary research"},
        {"text": "Pfizer Q2 2025: $14.2B revenue, 38% gross margin, 1,800 basis points vs consensus — primary filing", "label": "High-value: earnings + stats"},
        {"text": "Introduction to Semiconductor Industry: A general overview of chip manufacturing for beginners", "label": "Low-value: overview"},
        {"text": "What is Artificial Intelligence? Comprehensive tutorial for beginners covering AI concepts", "label": "Low-value: tutorial"},
    ]
    samples_data = json.dumps(sample_items)
    keywords_high = json.dumps(_PROPRIETARY_KWS)
    keywords_stats = json.dumps(_STATS_KWS)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>LM Tree — Bid Multiplier Discovery</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
:root {{
  --bg: #0d0f16;
  --surface: #13161f;
  --border: #1e2130;
  --border-bright: #2a2f45;
  --text: #d4d8e8;
  --muted: #5a6080;
  --green: #3ecf6e;
  --green-dim: #1a3828;
  --yellow: #f0b429;
  --yellow-dim: #2e2510;
  --red: #f06060;
  --red-dim: #2e1414;
  --accent: #6c8cff;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
  background: var(--bg); color: var(--text); min-height: 100vh;
  display: flex; flex-direction: column;
}}

/* ── Header ── */
header {{
  padding: 20px 28px 0;
  display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
}}
header h1 {{ font-size: 1rem; font-weight: 600; color: var(--text); letter-spacing: 0.01em; }}
header p  {{ font-size: 0.72rem; color: var(--muted); }}

/* ── Legend + controls row ── */
.toolbar {{
  padding: 10px 28px 14px;
  display: flex; align-items: center; gap: 20px; flex-wrap: wrap;
}}
.legend {{ display: flex; gap: 14px; }}
.leg {{ display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); }}
.leg-dot {{ width: 9px; height: 9px; border-radius: 2px; flex-shrink: 0; }}
.hint {{ margin-left: auto; font-size: 11px; color: var(--muted); }}
kbd {{
  background: var(--surface); border: 1px solid var(--border-bright);
  border-radius: 3px; padding: 1px 4px; font-size: 10px;
}}

/* ── Tree panel ── */
#tree-wrap {{
  flex: 1; overflow: auto; padding: 10px 0 20px;
  background: var(--bg);
}}
svg {{ display: block; margin: 0 auto; }}

/* links */
.link {{
  fill: none; stroke: var(--border-bright); stroke-width: 1.5px;
  transition: stroke .2s, stroke-width .2s;
}}
.link.active {{ stroke: var(--accent); stroke-width: 2.5px; }}

/* edge labels */
.edge-lbl {{
  font-size: 10px; font-weight: 600; fill: var(--muted);
  text-anchor: middle; pointer-events: none;
  transition: fill .2s;
}}
.edge-lbl.active {{ fill: var(--accent); }}

/* node cards */
.node {{ cursor: pointer; }}
.node rect.card {{
  stroke-width: 1px; stroke: var(--border-bright);
  rx: 10; ry: 10;
  transition: stroke .2s, filter .2s;
}}
.node.active rect.card {{ stroke: var(--accent); filter: drop-shadow(0 0 8px rgba(108,140,255,.35)); }}

/* color-strip */
.node rect.strip {{ rx: 0; ry: 0; }}

/* arm number */
.t-arm {{
  font-size: 22px; font-weight: 700;
  dominant-baseline: middle; text-anchor: middle;
}}
.t-sub {{
  font-size: 10px; fill: var(--muted);
  dominant-baseline: middle; text-anchor: middle;
}}
.t-count {{
  font-size: 10px; fill: var(--muted);
  dominant-baseline: middle; text-anchor: middle;
}}
.t-rule {{
  font-size: 9.5px; fill: #8892b0;
  dominant-baseline: middle; text-anchor: middle;
}}
.t-badge {{
  font-size: 9px; font-weight: 600; letter-spacing: .04em;
  dominant-baseline: middle; text-anchor: middle;
}}

/* divider line */
.divider {{ stroke: var(--border-bright); stroke-width: 1; }}

/* ── Sidebar (arm explorer) ── */
#sidebar {{
  position: fixed; top: 0; right: 0; bottom: 0; width: 280px;
  background: var(--surface); border-left: 1px solid var(--border);
  padding: 20px 18px; overflow-y: auto;
  transform: translateX(100%); transition: transform .25s ease;
  z-index: 20;
}}
#sidebar.open {{ transform: translateX(0); }}
#sidebar h2 {{ font-size: 12px; font-weight: 600; color: var(--text); margin-bottom: 4px; }}
#sidebar .sid-sub {{ font-size: 11px; color: var(--muted); margin-bottom: 16px; line-height: 1.4; }}
#sidebar-close {{
  position: absolute; top: 14px; right: 14px;
  background: none; border: none; color: var(--muted); font-size: 16px; cursor: pointer;
  line-height: 1;
}}
#sidebar-close:hover {{ color: var(--text); }}
.arm-row {{
  display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
}}
.arm-key {{
  width: 28px; font-size: 12px; font-weight: 600; color: var(--muted); flex-shrink: 0;
}}
.arm-key.opt {{ color: var(--text); }}
.arm-track {{
  flex: 1; background: #1a1d2a; border-radius: 4px; height: 10px; overflow: hidden;
}}
.arm-fill {{ height: 10px; border-radius: 4px; }}
.arm-meta {{ width: 56px; font-size: 10px; color: var(--muted); text-align: right; flex-shrink: 0; }}
.arm-meta.opt {{ color: var(--text); }}
.arm-star {{ margin-left: 2px; color: var(--accent); }}

/* ── Prediction playground ── */
#playground {{
  border-top: 1px solid var(--border);
  padding: 18px 28px 24px;
  background: var(--surface);
}}
#playground h2 {{ font-size: 12px; font-weight: 600; color: var(--text); margin-bottom: 4px; }}
#playground p  {{ font-size: 11px; color: var(--muted); margin-bottom: 12px; }}
.pg-row {{ display: flex; gap: 10px; align-items: stretch; }}
#pg-input {{
  flex: 1; background: var(--bg); border: 1px solid var(--border-bright);
  border-radius: 8px; padding: 9px 12px; font-size: 12px; color: var(--text);
  font-family: inherit; resize: none; min-height: 52px; outline: none;
  transition: border-color .15s;
}}
#pg-input:focus {{ border-color: var(--accent); }}
#pg-run {{
  background: var(--accent); color: #fff; border: none;
  border-radius: 8px; padding: 0 18px; font-size: 12px; font-weight: 600;
  cursor: pointer; white-space: nowrap; align-self: stretch;
  transition: opacity .15s;
}}
#pg-run:hover {{ opacity: .85; }}
.pg-samples {{ margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; }}
.pg-chip {{
  background: var(--bg); border: 1px solid var(--border-bright);
  border-radius: 20px; padding: 4px 10px; font-size: 10.5px; color: var(--muted);
  cursor: pointer; transition: border-color .15s, color .15s;
}}
.pg-chip:hover {{ border-color: var(--accent); color: var(--text); }}
#pg-result {{
  margin-top: 14px; display: none;
  background: var(--bg); border: 1px solid var(--border-bright);
  border-radius: 10px; padding: 14px 16px;
}}
#pg-result.show {{ display: block; }}
.pg-path {{ font-size: 11px; color: var(--muted); line-height: 1.8; }}
.pg-path .step {{ display: flex; align-items: flex-start; gap: 8px; }}
.pg-path .arrow {{ color: var(--accent); flex-shrink: 0; margin-top: 1px; }}
.pg-path .match {{ color: var(--green); }}
.pg-path .nomatch {{ color: var(--muted); }}
.pg-multiplier {{
  margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border);
  display: flex; align-items: center; gap: 12px;
}}
.pg-big {{ font-size: 28px; font-weight: 700; }}
.pg-right {{ font-size: 11px; color: var(--muted); line-height: 1.6; }}
.pg-example {{ color: var(--text); font-weight: 500; }}
</style>
</head>
<body>

<header>
  <h1>LM Tree — Bid Multiplier Discovery</h1>
  <p>Learned pricing tree from content citation outcomes</p>
</header>

<div class="toolbar">
  <div class="legend">
    <div class="leg"><div class="leg-dot" style="background:#3ecf6e"></div>arm ≥ 0.8 high-value</div>
    <div class="leg"><div class="leg-dot" style="background:#f0b429"></div>arm 0.5–0.8 mid-value</div>
    <div class="leg"><div class="leg-dot" style="background:#f06060"></div>arm &lt; 0.5 low-value</div>
  </div>
  <span class="hint">Click a node to open arm explorer &nbsp;·&nbsp; <kbd>Esc</kbd> to close</span>
</div>

<div id="tree-wrap"></div>

<!-- Sidebar: arm exploration detail -->
<div id="sidebar">
  <button id="sidebar-close">✕</button>
  <h2 id="sid-title">Node</h2>
  <div class="sid-sub" id="sid-rule"></div>
  <div id="sid-arms"></div>
</div>

<!-- Prediction playground -->
<div id="playground">
  <h2>Try it — predict bid multiplier for any content</h2>
  <p>Type or paste a content snippet below and see which leaf node it lands on.</p>
  <div class="pg-row">
    <textarea id="pg-input" placeholder="Paste a title + snippet here…"></textarea>
    <button id="pg-run">Predict →</button>
  </div>
  <div class="pg-samples" id="pg-samples"></div>
  <div id="pg-result">
    <div class="pg-path" id="pg-path-steps"></div>
    <div class="pg-multiplier">
      <div class="pg-big" id="pg-mult-val"></div>
      <div class="pg-right" id="pg-mult-detail"></div>
    </div>
  </div>
</div>

<script>
// ── Data ─────────────────────────────────────────────────────────────────────
const RAW     = {tree_data};
const SAMPLES = {samples_data};
const KW_PROP = {keywords_high};
const KW_STAT = {keywords_stats};

function kwMatch(text, keywords) {{
  const t = text.toLowerCase();
  return keywords.some(k => t.includes(k.toLowerCase()));
}}

// ── Arm coloring ─────────────────────────────────────────────────────────────
function armBg(arm) {{
  if (arm >= 0.8) return "var(--green-dim)";
  if (arm >= 0.5) return "var(--yellow-dim)";
  return "var(--red-dim)";
}}
function armStrip(arm) {{
  if (arm >= 0.8) return "var(--green)";
  if (arm >= 0.5) return "var(--yellow)";
  return "var(--red)";
}}
function armText(arm) {{
  if (arm >= 0.8) return "var(--green)";
  if (arm >= 0.5) return "var(--yellow)";
  return "var(--red)";
}}

// ── Node sizing ───────────────────────────────────────────────────────────────
const NW = 210;            // node width
const NH_LEAF  = 80;       // leaf height
const NH_SPLIT = 118;      // split node height (extra room for rule text)
const STRIP_W  = 5;
const H_GAP = 50;
const V_GAP = 110;

function nodeH(d) {{ return d.data.is_leaf ? NH_LEAF : NH_SPLIT; }}

// ── Text wrap helper ─────────────────────────────────────────────────────────
function wrapText(text, maxChars) {{
  const words = text.split(" ");
  const lines = [];
  let cur = "";
  for (const w of words) {{
    const test = cur ? cur + " " + w : w;
    if (test.length > maxChars) {{ if (cur) lines.push(cur); cur = w; }}
    else cur = test;
  }}
  if (cur) lines.push(cur);
  return lines;
}}

// ── D3 hierarchy ─────────────────────────────────────────────────────────────
const hierRoot = d3.hierarchy(RAW, d => d.children);
hierRoot.each(d => {{ d._children = null; }});

// ── SVG setup ────────────────────────────────────────────────────────────────
const container = document.getElementById("tree-wrap");
const svg = d3.select(container).append("svg");
const g   = svg.append("g");
const gLinks = g.append("g").attr("class", "links-layer");
const gNodes = g.append("g").attr("class", "nodes-layer");

// Active path node IDs (for highlighting)
let activePath = new Set();

function update() {{
  const treeLayout = d3.tree()
    .nodeSize([NW + H_GAP, NH_SPLIT + V_GAP])
    .separation((a, b) => a.parent === b.parent ? 1 : 1.2);

  treeLayout(hierRoot);

  const nodes = hierRoot.descendants();
  const links = hierRoot.links();

  // Resize SVG to fit
  const xs = nodes.map(d => d.x), ys = nodes.map(d => d.y);
  const pad = 40;
  const minX = Math.min(...xs) - NW/2 - pad;
  const maxX = Math.max(...xs) + NW/2 + pad;
  const maxY = Math.max(...ys) + NH_SPLIT + pad;
  svg.attr("width", maxX - minX).attr("height", maxY);
  g.attr("transform", `translate(${{-minX}}, ${{pad / 2}})`);

  // ── Links ──
  const linkSel = gLinks.selectAll(".link").data(links, d => d.target.data.id);

  const linkEnter = linkSel.enter().append("path").attr("class", "link");
  linkSel.merge(linkEnter)
    .attr("class", d => "link" + (activePath.has(d.source.data.id) && activePath.has(d.target.data.id) ? " active" : ""))
    .attr("d", d => {{
      const sx = d.source.x, sy = d.source.y + nodeH(d.source);
      const tx = d.target.x, ty = d.target.y;
      const my = (sy + ty) / 2;
      return `M${{sx}},${{sy}} C${{sx}},${{my}} ${{tx}},${{my}} ${{tx}},${{ty}}`;
    }});
  linkSel.exit().remove();

  // ── Edge labels ──
  const eLblSel = gLinks.selectAll(".edge-lbl").data(links, d => d.target.data.id);
  const eLblEnter = eLblSel.enter().append("text").attr("class", "edge-lbl");
  eLblSel.merge(eLblEnter)
    .attr("class", d => "edge-lbl" + (activePath.has(d.source.data.id) && activePath.has(d.target.data.id) ? " active" : ""))
    .attr("x", d => (d.source.x + d.target.x) / 2)
    .attr("y", d => (d.source.y + nodeH(d.source) + d.target.y) / 2)
    .text(d => d.target.data.edge_label || "");
  eLblSel.exit().remove();

  // ── Nodes ──
  const nodeSel = gNodes.selectAll(".node").data(nodes, d => d.data.id);

  const nodeEnter = nodeSel.enter().append("g")
    .attr("class", d => "node" + (activePath.has(d.data.id) ? " active" : ""))
    .attr("transform", d => `translate(${{d.x - NW/2}},${{d.y}})`)
    .on("click", (_, d) => {{
      if (d.children) {{ d._children = d.children; d.children = null; }}
      else if (d._children) {{ d.children = d._children; d._children = null; }}
      else {{ openSidebar(d.data); return; }}
      update();
    }})
    .on("dblclick", (_, d) => openSidebar(d.data));

  // Background card
  nodeEnter.append("rect").attr("class", "card")
    .attr("width", NW)
    .attr("fill", d => armBg(d.data.optimal_arm));

  // Color strip (left)
  nodeEnter.append("rect").attr("class", "strip")
    .attr("width", STRIP_W)
    .attr("fill", d => armStrip(d.data.optimal_arm));

  // Arm value
  nodeEnter.append("text").attr("class", "t-arm")
    .attr("x", NW / 2 + 12).attr("y", 30)
    .attr("fill", d => armText(d.data.optimal_arm))
    .text(d => "×" + d.data.optimal_arm.toFixed(1));

  // "bid multiplier" sub-label
  nodeEnter.append("text").attr("class", "t-sub")
    .attr("x", NW / 2 + 12).attr("y", 44)
    .text("bid multiplier");

  // Item count
  nodeEnter.append("text").attr("class", "t-count")
    .attr("x", NW / 2 + 12).attr("y", 57)
    .text(d => d.data.n_items + " training items");

  // Divider + rule section (split nodes only)
  nodeEnter.each(function(d) {{
    if (d.data.is_leaf) return;
    const g2 = d3.select(this);

    // Divider
    g2.append("line").attr("class", "divider")
      .attr("x1", STRIP_W + 6).attr("x2", NW - 6)
      .attr("y1", 68).attr("y2", 68);

    // "SPLIT ON" micro label
    g2.append("text").attr("class", "t-badge")
      .attr("x", NW / 2 + 12).attr("y", 78)
      .attr("fill", "var(--muted)")
      .text("SPLIT ON");

    // Rule text (wrapped, 2 lines max)
    const rule = d.data.split_rule || "";
    const lines = wrapText(rule, 26).slice(0, 2);
    lines.forEach((line, i) => {{
      g2.append("text").attr("class", "t-rule")
        .attr("x", NW / 2 + 12)
        .attr("y", 91 + i * 13)
        .text(line + (i === 1 && lines.length < wrapText(rule, 26).length ? "…" : ""));
    }});

    // Collapse hint
    g2.append("text").attr("class", "t-badge")
      .attr("x", NW - 12).attr("y", 8)
      .attr("fill", "var(--muted)").attr("text-anchor", "end")
      .text(d.children ? "▼" : "▶");
  }});

  // Leaf badge
  nodeEnter.each(function(d) {{
    if (!d.data.is_leaf) return;
    d3.select(this).append("text").attr("class", "t-badge")
      .attr("x", NW - 12).attr("y", 8)
      .attr("fill", "var(--muted)").attr("text-anchor", "end")
      .text("LEAF");
  }});

  // Set card heights after content is known
  nodeEnter.select("rect.card").attr("height", d => nodeH(d));
  nodeEnter.select("rect.strip").attr("height", d => nodeH(d));

  // Merge + update active class
  nodeSel.merge(nodeEnter)
    .attr("class", d => "node" + (activePath.has(d.data.id) ? " active" : ""))
    .attr("transform", d => `translate(${{d.x - NW/2}},${{d.y}})`);

  nodeSel.exit().remove();
}}

// ── Sidebar ───────────────────────────────────────────────────────────────────
function openSidebar(data) {{
  document.getElementById("sid-title").textContent =
    (data.is_leaf ? "Leaf: " : "Split: ") + data.id;
  document.getElementById("sid-rule").textContent =
    data.split_rule ? `Splits on: "${{data.split_rule}}"` : `${{data.n_items}} items · no further split`;

  const stats = data.arm_stats || [];
  const maxV = Math.max(...stats.map(s => s.value), 0.001);
  document.getElementById("sid-arms").innerHTML = stats.map(s => {{
    const isOpt = s.arm === data.optimal_arm;
    const pct = Math.round(s.value / maxV * 100);
    return `<div class="arm-row">
      <div class="arm-key ${{isOpt ? "opt" : ""}}">${{s.arm.toFixed(1)}}${{isOpt ? `<span class="arm-star">★</span>` : ""}}</div>
      <div class="arm-track"><div class="arm-fill" style="width:${{pct}}%;background:${{isOpt ? armStrip(s.arm) : "#2a2f45"}}"></div></div>
      <div class="arm-meta ${{isOpt ? "opt" : ""}}">${{(s.citation_rate*100).toFixed(0)}}% cite</div>
    </div>`;
  }}).join("");

  document.getElementById("sidebar").classList.add("open");
}}

document.getElementById("sidebar-close").onclick = () =>
  document.getElementById("sidebar").classList.remove("open");
document.addEventListener("keydown", e => {{
  if (e.key === "Escape") document.getElementById("sidebar").classList.remove("open");
}});

// ── Prediction playground ─────────────────────────────────────────────────────
function traverseTree(node, text) {{
  const path = [node.id];
  let cur = node;
  const steps = [];

  while (!cur.is_leaf && cur.children && cur.children.length >= 2) {{
    const rule = cur.split_rule || "";
    const kws = rule.includes("proprietary") ? KW_PROP : KW_STAT;
    const matched = kws.some(k => text.toLowerCase().includes(k.toLowerCase()));
    steps.push({{ rule, matched, from: cur.id }});
    cur = matched ? cur.children[0] : cur.children[1];
    path.push(cur.id);
  }}

  return {{ leafId: cur.id, arm: cur.optimal_arm, steps, path }};
}}

function runPredict() {{
  const text = document.getElementById("pg-input").value.trim();
  if (!text) return;

  const result = traverseTree(RAW, text);
  activePath = new Set(result.path);
  update();

  // Build path HTML
  let html = `<div class="step"><span class="arrow">▶</span><span>Start at root node</span></div>`;
  result.steps.forEach(s => {{
    const cls = s.matched ? "match" : "nomatch";
    const verb = s.matched ? "✓ matches" : "✗ no match for";
    html += `<div class="step"><span class="arrow">→</span>
      <span><span class="${{cls}}">${{verb}}</span>&nbsp;&nbsp;<em>"${{s.rule}}"</em></span>
    </div>`;
  }});
  html += `<div class="step"><span class="arrow">⬡</span><span>Arrive at leaf <strong>${{result.leafId}}</strong></span></div>`;
  document.getElementById("pg-path-steps").innerHTML = html;

  const arm = result.arm;
  const eg = (0.35 * arm).toFixed(3);
  document.getElementById("pg-mult-val").textContent = "×" + arm.toFixed(1);
  document.getElementById("pg-mult-val").style.color = armStrip(arm);
  document.getElementById("pg-mult-detail").innerHTML =
    `<span class="pg-example">bid multiplier</span><br>e.g. $0.35 ceiling → <strong>bid $${{eg}}</strong>`;

  document.getElementById("pg-result").classList.add("show");
}}

document.getElementById("pg-run").onclick = runPredict;
document.getElementById("pg-input").addEventListener("keydown", e => {{
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) runPredict();
}});

// Sample chips
const samplesEl = document.getElementById("pg-samples");
SAMPLES.forEach(s => {{
  const chip = document.createElement("button");
  chip.className = "pg-chip";
  chip.textContent = s.label;
  chip.onclick = () => {{
    document.getElementById("pg-input").value = s.text;
    runPredict();
  }};
  samplesEl.appendChild(chip);
}});

// Init
update();
</script>
</body>
</html>"""

    Path(out_path).write_text(html)
    return out_path


# ── Synthetic training corpus ─────────────────────────────────────────────────

def build_demo_corpus() -> list[ContentItem]:
    """
    30 synthetic content items with two planted value clusters:
      High-WTP (~15): proprietary/primary-research signals → y ~ Bernoulli(0.85)
      Low-WTP  (~15): overview/tutorial signals            → y ~ Bernoulli(0.25)
    A few items from valyu_results.json are also seeded in.
    """
    rng = random.Random(RANDOM_SEED)

    high_templates = [
        ("Goldman Sachs: Proprietary semiconductor demand forecast Q2 2025 — includes 47 exclusive data series and internal supply-chain survey results.", "https://gs.com/research/semiconductors-q2-2025", 0.9),
        ("Pfizer internal benchmark: Phase-III clinical trial data for oncology pipeline, primary research, 1,200 patients, statistically significant outcomes.", "https://pfizer.com/trials/onco-2025", 0.85),
        ("Bloomberg Intelligence exclusive: NVIDIA earnings per share model with proprietary GPU shipment data, 12-month forward projections, licensed dataset.", "https://bloomberg.com/bi/nvidia-eps-2025", 0.9),
        ("Morgan Stanley primary research: Exclusive survey of 300 CIOs on AI infrastructure spend. Proprietary dataset, not available elsewhere.", "https://morganstanley.com/research/ai-cio-survey", 0.88),
        ("Federal Reserve embargoed data release: Q4 consumer credit statistics, 97 basis points increase, internal Fed figures pre-publication.", "https://federalreserve.gov/releases/consumer-credit-q4", 0.95),
        ("McKinsey & Company: First-party global supply chain benchmark — 2,400 companies surveyed, proprietary index, exclusive annual report.", "https://mckinsey.com/supply-chain-benchmark-2025", 0.85),
        ("SEC filing: Apple 10-Q with detailed earnings breakdown, $97.8B revenue, 42.3% gross margin, primary financial disclosure document.", "https://sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=AAPL", 0.9),
        ("JPMorgan exclusive: Proprietary quant model for rates — yield curve inversion signals, 220 bps spread, internal fixed-income research.", "https://jpmorgan.com/research/rates-model-2025", 0.87),
        ("Gartner primary survey: Exclusive enterprise AI adoption data, 1,500 respondents, licensed market forecast through 2027, internal dataset.", "https://gartner.com/en/research/ai-adoption-2027", 0.85),
        ("Nature Medicine: First-in-human clinical trial results, pre-release embargo lifted — CRISPR gene-editing primary research, n=450.", "https://nature.com/articles/nm-2025-crispr-phase1", 0.9),
        ("IDC proprietary forecast: Global semiconductor revenue quarterly tracker, 18 data series, licensed database, exclusive analyst note.", "https://idc.com/research/semiconductor-tracker-q2-2025", 0.85),
        ("Tesla Q2 2025 earnings flash: primary disclosure, 412,000 deliveries, $2.31 EPS, $0.8B free cash flow — straight from earnings release.", "https://ir.tesla.com/financial-information/sec-filings", 0.88),
        ("Palantir internal whitepaper: Proprietary AI platform benchmark on classified defense datasets — exclusive pre-release for institutional investors.", "https://palantir.com/research/aip-benchmark-2025", 0.82),
        ("WHO primary epidemiological data: Outbreak surveillance report, 47 countries, primary case counts from official national health authorities.", "https://who.int/publications/surveillance-2025", 0.87),
        ("S&P Global: Exclusive corporate credit ratings update — 214 downgrades Q2 2025, proprietary default probability model, licensed data.", "https://spglobal.com/ratings/corporate-defaults-q2-2025", 0.9),
    ]

    low_templates = [
        ("What is Artificial Intelligence? A general overview of AI concepts, history, and use cases. Introduction to machine learning for beginners.", "https://coursera.org/articles/what-is-artificial-intelligence", 0.2),
        ("Introduction to Semiconductor Industry: A comprehensive guide to how chips are manufactured. Overview of supply chains and fabrication basics.", "https://chipguide.io/intro-semiconductors", 0.15),
        ("What is a bond? General explainer covering fixed income basics, yield curves, and duration. Summary of bond market fundamentals.", "https://investopedia.com/terms/b/bond.asp", 0.25),
        ("Understanding machine learning: Tutorial covering supervised vs. unsupervised learning, common algorithms, and introductory examples.", "https://towardsdatascience.com/ml-basics-2025", 0.2),
        ("What is cloud computing? Overview of IaaS, PaaS, and SaaS. General summary of major cloud providers and their pricing tiers.", "https://ibm.com/cloud/learn/cloud-computing", 0.15),
        ("The history of the Federal Reserve: A general educational summary of the Fed's founding, mandate, and policy tools. Overview article.", "https://federalreservehistory.org/overview", 0.25),
        ("Introduction to CRISPR gene editing: A beginner's guide to how CRISPR works, applications in medicine, and ethical considerations.", "https://genome.gov/about-genomics/fact-sheets/CRISPR", 0.2),
        ("What are earnings per share (EPS)? General tutorial explaining EPS calculation, why it matters, and how to interpret quarterly results.", "https://investopedia.com/terms/e/eps.asp", 0.15),
        ("Overview of supply chain management: An introductory guide covering logistics, procurement, and distribution in modern businesses.", "https://supplychain101.com/overview", 0.25),
        ("What is a clinical trial? General explainer covering Phase I–IV trials, regulatory requirements, and patient enrollment basics.", "https://clinicaltrials.gov/about-site/what-is-a-clinical-trial", 0.2),
        ("AI tools for businesses in 2025: A general summary of popular AI software, productivity tools, and how companies are adopting AI.", "https://techradar.com/best/ai-tools-for-business", 0.25),
        ("What is the S&P 500? Introduction to the index, its history, composition methodology, and how to invest. General overview for beginners.", "https://investopedia.com/terms/s/sp500.asp", 0.2),
        ("Understanding corporate bonds: Tutorial on investment-grade vs. high-yield, credit ratings basics, and general bond market overview.", "https://fidelity.com/learning-center/corporate-bonds", 0.15),
        ("What is Tesla? Company overview covering history, products, market position, and a general summary of its EV business.", "https://en.wikipedia.org/wiki/Tesla,_Inc.", 0.2),
        ("Introduction to epidemiology: What is disease surveillance? A general educational overview of how public health agencies track outbreaks.", "https://cdc.gov/training/epidemiology-intro", 0.25),
    ]

    corpus: list[ContentItem] = []

    for title, url, base_p in high_templates:
        y = 1.0 if rng.random() < base_p else 0.0
        corpus.append(ContentItem(id=str(uuid.uuid4())[:8], text=title, url=url, y=y))

    for title, url, base_p in low_templates:
        y = 1.0 if rng.random() < base_p else 0.0
        corpus.append(ContentItem(id=str(uuid.uuid4())[:8], text=title, url=url, y=y))

    # Optionally seed a couple from valyu_results.json
    valyu_path = Path(__file__).parent / "valyu_results.json"
    if valyu_path.exists():
        raw = json.loads(valyu_path.read_text())
        for entry in raw[:3]:
            preview = entry.get("content_preview", "") or entry.get("title", "")
            text = (entry.get("title", "") + ". " + preview[:150]).strip()
            y = 1.0 if rng.random() < 0.3 else 0.0  # web overviews → low citation
            corpus.append(ContentItem(id=str(uuid.uuid4())[:8], text=text, url=entry.get("url", ""), y=y))

    rng.shuffle(corpus)
    return corpus


# ── Demo runner ───────────────────────────────────────────────────────────────

SAMPLE_PREDICTIONS = [
    (
        "Goldman Sachs: Proprietary semiconductor demand forecast Q2 2025 — exclusive 47-series dataset, licensed.",
        "Expected: HIGH multiplier (proprietary + quantified data)",
    ),
    (
        "Morgan Stanley: Exclusive survey of 300 CIOs, primary research, AI infrastructure spend proprietary dataset.",
        "Expected: HIGH multiplier (proprietary + primary research)",
    ),
    (
        "Introduction to Semiconductor Industry: A general overview of chip manufacturing for beginners.",
        "Expected: LOW multiplier (overview content)",
    ),
    (
        "What is Artificial Intelligence? Comprehensive tutorial for beginners covering AI concepts and history.",
        "Expected: LOW multiplier (tutorial/overview)",
    ),
    (
        "Pfizer Q2 2025 earnings: $14.2B revenue, 38% gross margin, 1,800 basis points vs consensus — primary filing.",
        "Expected: HIGH multiplier (earnings + quantified stats)",
    ),
]


def run_demo() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    mode = "Claude API" if api_key else "keyword heuristics (set ANTHROPIC_API_KEY for LLM-based discovery)"
    print(f"\n{'='*62}")
    print(f"  LM Tree — Buyer-Side Bid Multiplier Demo")
    print(f"  Feature discovery: {mode}")
    print(f"{'='*62}\n")

    corpus = build_demo_corpus()
    cited = sum(1 for it in corpus if it.y >= 0.5)
    print(f"Training corpus: {len(corpus)} items  ({cited} cited, {len(corpus)-cited} not cited)\n")

    print("── Growing tree ────────────────────────────────────────────")
    root = TreeNode(node_id="ROOT", depth=0, items=corpus)
    root = grow_node(root, max_depth=MAX_DEPTH)

    print("\n── Final tree ──────────────────────────────────────────────")
    print_tree(root)

    print("\n── Sample predictions ──────────────────────────────────────")
    for text, note in SAMPLE_PREDICTIONS:
        multiplier, path = predict(root, text)
        print(f"\n  [{note}]")
        print(f"  Text: \"{text[:90]}...\"" if len(text) > 90 else f"  Text: \"{text}\"")
        for step in path:
            print(f"    {step}")
        print(f"  → Bid multiplier: {multiplier:.1f}  (e.g. $0.35 ceiling → bid ${0.35*multiplier:.3f})")

    print("\n── Generating visualization ────────────────────────────────")
    out = generate_viz(root, "lm_tree_viz.html")
    print(f"  Saved: {out}")
    webbrowser.open(f"file://{Path(out).resolve()}")
    print("  Browser opened. Click nodes to collapse; hover for arm chart.")
    print(f"\n{'='*62}\n")


if __name__ == "__main__":
    run_demo()
