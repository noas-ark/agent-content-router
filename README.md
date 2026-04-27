# bootk.ai

Demand-side intelligence for AI content purchasing. A deep-research pipeline that decomposes a query into sub-queries, searches free sources, scores coverage, and surfaces a purchase plan for any gaps that paid content can fill.

## Overview

bootk.ai sits between an incoming user query and the content market. It runs a full research pipeline — signals → sub-queries → free search & RAG → coverage scoring → paid source probing → plan & answer — and lets the user selectively purchase paid content to close coverage gaps.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env   # add your API keys
python app.py
```

Open **http://127.0.0.1:5001**

## Pipeline

| Step | What happens |
|------|-------------|
| **Signals** | Extracts intent, stakes, freshness, depth, and credibility from the query |
| **Sub-queries** | Planner (DeepReason-style) decomposes into distinct search facets; embedding dedup (cosine ≥ 0.88) removes near-duplicates |
| **Search & RAG** | Brave free-tier search → full-page fetch → sentence-transformer chunking → coverage score per result |
| **Price fetch** | For sub-queries below the quality floor (GAP), probes paid sources and returns a cost estimate |
| **Plan & buy** | Gap cards with one-click paid fetch; coverage line updates with actual score after fetch; re-synthesizes the answer |
| **Answer** | Writer LLM synthesises all gathered evidence into a final answer |

## UI tabs

- **Signals** — Extracted query signals, intent, routing decisions
- **Subqueries** — Decomposed search tasks with coverage status per sub-query
- **Search** — Free-tier search hits with RAG relevance badges; GAP pill links to Price fetch
- **Price fetch** — Paid source quotes per GAP sub-query; "Plan & buy →" button jumps to purchase
- **Plan & buy** — Free sources summary (X/N covered), gap cards with fetch buttons, projected cost vs all-paid comparison
- **Bidding** — Paywalled content by sub-query (HTTP 403/401 and known-paywall domains only), bid ceiling per gap
- **Answer** — Synthesised answer; updates after paid fetches via `/re-synthesize`

## Features

- **Coverage scoring** — Per-result relevance via sentence-transformers (`all-MiniLM-L6-v2`); best free hit compared against signal-derived quality floor
- **Gap detection** — Sub-queries below the quality floor trigger a paid source estimate; gap cards show "fetch could add +Xpp" before purchase, actual gain after
- **Embedding dedup** — Planner output is deduplicated using cosine similarity before search; catches near-duplicate facets the LLM instruction misses
- **Research critic** — LLM (gpt-4o-mini) scores completeness 0–1 from gathered snippets; heuristic fallback when no OpenAI key
- **Re-synthesis** — After a paid fetch, the writer re-runs over free + paid results and updates the answer in place
- **Homepage filter** — Drops search results whose URL path is empty or a single generic slug (e.g. `/news`, `/markets`)

## Environment

| Variable | Description |
|----------|-------------|
| `EXA_API_KEY` | Paid source fetch (required for price fetch tab) |
| `DSAIL_BRAVE_API_KEY` | Free-tier web search |
| `DSAIL_OPENAI_API_KEY` | Writer synthesis + research critic |
| `DSAIL_VALYU_API_KEY` | Valyu content probing |
| `WORLD_NEWS_API_KEY` | World News API source |
| `MAX_VALYU_PROBES_PER_REQUEST` | Cap on Valyu probes per optimize call (default: 30) |

Copy `.env.example` and fill in the keys you have. The pipeline degrades gracefully — free search and RAG work without OpenAI or Exa keys; the critic and writer fall back to heuristics.

## API

Interactive docs at **http://127.0.0.1:5001/api-reference**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/optimize` | POST | Full pipeline: signals → sub-queries → search → plan |
| `/fetch-exa` | POST | On-demand paid fetch for a single sub-query |
| `/re-synthesize` | POST | Re-run writer with free + cached paid results |
| `/feedback` | POST | Submit outcome feedback |

## Run options

```bash
python app.py              # default port 5001
python app.py --port 8080  # custom port
```

Default port 5001 avoids conflicts with macOS AirPlay on 5000.
