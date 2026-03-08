# SourceRoute

Demand-side intelligence for AI content purchasing. Signal-based purchase optimizer that takes priced content candidates and outputs an optimal purchase plan and per-source bids.

## Overview

SourceRoute is the DSP equivalent for the AI content market—like The Trade Desk for digital advertising. It sits between an incoming user query and the content market, evaluating each purchase opportunity against query signals before committing spend.

- **Content routing** — Scores sources on relevance, credibility, freshness, and depth; selects an optimal purchase plan within budget
- **Bidding workflow** — DSP-style bids per source: `our_bid = ask × (0.72 + 0.28 × utility)`; buy if bid ≥ ask, else pass
- **Real-time search** — Backs the plan with articles from Brave, Google CSE, or DuckDuckGo; shows all search results (BBC, CNN, etc.) with catalog annotation when applicable

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env   # optional: add BRAVE_API_KEY or GOOGLE_CSE_*
python app.py
```

Open **http://127.0.0.1:5001**

## Features

- **Query signal extraction** — Intent, stakes, freshness, depth, credibility (4-dimension framework)
- **Purchase plan** — Selected sources, cost comparison, ROI at scale
- **Bidding tab** — Per-source bids, value ceiling, click/hover for calculation details and anonymized other-bidder data
- **Articles to scrape** — Real search results mapped to the plan; catalog domains get name + price, others get domain label
- **Learning** — Outcomes via `/feedback`; learned publisher performance via `/learn`
- **Admin** — Metrics, conversion events, feedback dashboard

## Search providers

Search is used to back the purchase plan with real articles. Provider order with **fallback** (first to return results wins):

1. **Brave** — `BRAVE_API_KEY` in `.env`
2. **Google Custom Search** — `GOOGLE_CSE_API_KEY` + `GOOGLE_CSE_CX`
3. **DuckDuckGo** — via `ddgs` package (no key, always available)

Set keys in `.env` for reliable search; DuckDuckGo works without keys but may be rate-limited.

### Google Custom Search keys

1. Create a [Programmable Search Engine](https://programmablesearchengine.google.com) and copy the **Search engine ID** → `GOOGLE_CSE_CX`
2. Enable [Custom Search API](https://console.cloud.google.com/) and create an API key → `GOOGLE_CSE_API_KEY`
3. Add both to `.env`

## API Reference

Interactive API docs at **http://127.0.0.1:5001/api-reference** (or click **API Reference** in the topbar).

Endpoints relevant to content routing and bidding:

| Endpoint      | Method | Description                                           |
|---------------|--------|-------------------------------------------------------|
| `/optimize`   | POST   | Optimize purchase plan; returns signals, selected sources, bids, articles |
| `/feedback`   | POST   | Submit outcome feedback (event_id, sources_cited, quality) |
| `/learn`      | GET    | Learned publisher performance by query cluster       |

## Environment

| Variable           | Description                                      |
|--------------------|--------------------------------------------------|
| `BRAVE_API_KEY`    | Brave Search API token (recommended for search)  |
| `GOOGLE_CSE_API_KEY` | Google Custom Search API key                   |
| `GOOGLE_CSE_CX`    | Google Custom Search engine ID                   |
| `LEARNING_DB`      | Path to SQLite DB for learning (default: learning.db) |

## Run options

```bash
python app.py              # default port 5001
python app.py --port 8080  # custom port
```

Default port 5001 avoids conflicts with macOS AirPlay on 5000.
