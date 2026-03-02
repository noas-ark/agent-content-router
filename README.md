# agent-content-router

Signal-based purchase optimizer for AI agents navigating the paid content web.

## Overview

As AI agents become the primary consumers of the web, the content tech stack is shifting from human-centric ad models to agent-centric licensing and metering. This project implements a **Purchase Optimizer** that takes priced content candidates (via TollBit, Cloudflare 402, or Microsoft PCM) and outputs an optimal purchase plan.
The goal is to maximize expected answer quality and signal depth per dollar while staying within budget and time constraints.

### Backing the plan with real articles (search)

The purchase plan can be backed by **real-time search results**: the app runs your query against a web search API and maps result URLs to the catalog (Bloomberg, WSJ, etc.), so you see actual articles to scrape behind each paywall.

**Provider order (first configured is used):** Brave → Google CSE → DuckDuckGo (no key, always fallback).

You always get real articles (DuckDuckGo by default). Add keys in `.env` to use Brave or Google.

### How to get Google Custom Search keys

You need **two** values: an API key and a Search Engine ID (CX).

1. **Create a Programmable Search Engine**
   - Go to [programmablesearchengine.google.com](https://programmablesearchengine.google.com).
   - Click **Add** and create a search engine (e.g. “Search the entire web” or add specific sites).
   - After it’s created, open it and copy the **Search engine ID** (looks like `a1b2c3d4e5f6g7h8i`) → this is **GOOGLE_CSE_CX**.

2. **Create an API key**
   - Go to [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
   - Enable **Custom Search API**: APIs & Services → Library → search for “Custom Search API” → Enable.
   - Create a key: APIs & Services → Credentials → Create credentials → API key.
   - Copy the key → this is **GOOGLE_CSE_API_KEY**.

3. **Put them in `.env`:**
   ```bash
   GOOGLE_CSE_API_KEY=your_api_key_here
   GOOGLE_CSE_CX=your_search_engine_id_here
   ```

Google gives [100 free queries/day](https://developers.google.com/custom-search/v1/overview#pricing) for Custom Search.

