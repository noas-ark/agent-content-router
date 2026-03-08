# bootk.ai

Demand-side intelligence for AI content purchasing. Signal-based purchase optimizer that takes priced content candidates and outputs an optimal purchase plan and per-source bids.

## Overview

bootk.ai is the DSP equivalent for the AI content market—like The Trade Desk for digital advertising. It sits between an incoming user query and the content market, evaluating each purchase opportunity against query signals before committing spend.

- **Content routing** — Scores sources on relevance, credibility, freshness, and depth; selects an optimal purchase plan within budget
- **Bidding workflow** — DSP-style bids per source: `our_bid = ask × (0.72 + 0.28 × utility)`; buy if bid ≥ ask, else pass

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5001**

## Features

- **Query signal extraction** — Intent, stakes, freshness, depth, credibility (4-dimension framework)
- **Purchase plan** — Selected sources, cost comparison
- **Bidding tab** — Per-source bids, value ceiling, click/hover for calculation details and anonymized other-bidder data
- **Learning** — Outcomes via `/feedback`; learned publisher performance via `/learn`
- **Admin** — Metrics, conversion events, feedback dashboard

## Search (currently disabled)

The Articles to scrape feature (search-backed results) is commented out. No search API keys are required.

To re-enable: uncomment the search block in `app.py` (optimize_route) and the Articles to scrape section in `index.html`. Then you can optionally add `BRAVE_API_KEY` or `GOOGLE_CSE_API_KEY` + `GOOGLE_CSE_CX` in `.env`.

## API Reference

Interactive API docs at **http://127.0.0.1:5001/api-reference** (or click **API Reference** in the topbar).

Endpoints relevant to content routing and bidding:

| Endpoint      | Method | Description                                           |
|---------------|--------|-------------------------------------------------------|
| `/optimize`   | POST   | Optimize purchase plan; returns signals, selected sources, bids |
| `/feedback`   | POST   | Submit outcome feedback (event_id, sources_cited, quality) |
| `/learn`      | GET    | Learned publisher performance by query cluster       |

## Environment

| Variable     | Description                                      |
|--------------|--------------------------------------------------|
| `LEARNING_DB` | Path to SQLite DB for learning (default: learning.db) |

No API keys required. Search keys (`BRAVE_API_KEY`, `GOOGLE_CSE_*`) are only needed if you uncomment the search feature.

## Run options

```bash
python app.py              # default port 5001
python app.py --port 8080  # custom port
```

Default port 5001 avoids conflicts with macOS AirPlay on 5000.
