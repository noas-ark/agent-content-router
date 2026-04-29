"""
Query Decomposition Comparison
-------------------------------
Compares how two open source search agents break down a research
task into search-ready sub-queries. Uses their ACTUAL prompts,
extracted from source — not approximations.

Approaches tested:
  1. GPT-Researcher  — generate_search_queries_prompt() from
                       gpt_researcher/prompts/__init__.py
  2. Deep Research   — generate_search_queries() from
                       gpt_researcher/skills/deep_research.py

Both stop at decomposition. No search, no scraping, no synthesis.

Run:
    export OPENAI_API_KEY=sk-...
    python compare_decomp.py
"""

import os
import json
from datetime import datetime, timezone
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
MODEL = "gpt-4o-mini"

TASK = (
    "Find prior art for a patent on transformer attention mechanisms "
    "that could invalidate claim 3 — multi-head attention with learned "
    "positional encodings, filed June 2017"
)

N_QUERIES = 5


def gpt_researcher_decompose(task: str, n: int = N_QUERIES) -> list[str]:
    """
    GPT-Researcher's exact prompt from:
    gpt_researcher/prompts/__init__.py → generate_search_queries_prompt()

    Designed for Google search — returns short keyword strings.
    """
    dynamic_example = ", ".join([f'"query {i+1}"' for i in range(n)])

    prompt = (
        f'Write {n} google search queries to search online that form an '
        f'objective opinion from the following task: "{task}"\n\n'
        f'Assume the current date is '
        f'{datetime.now(timezone.utc).strftime("%B %d, %Y")} if required.\n\n'
        f'You must respond with a list of strings in the following format: '
        f'[{dynamic_example}].\n'
        f'The response should contain ONLY the list.'
    )

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )

    raw = resp.choices[0].message.content.strip()
    try:
        return [q.strip() for q in json.loads(raw) if isinstance(q, str)]
    except Exception:
        return [
            line.strip().strip('"').strip("'")
            for line in raw.split("\n")
            if line.strip() and line.strip() not in ["[", "]"]
        ]


def deep_research_decompose(task: str, n: int = N_QUERIES) -> list[dict]:
    """
    Deep Research skill's exact prompt from:
    gpt_researcher/skills/deep_research.py → generate_search_queries()

    Returns query + research goal per sub-query.
    Goal explains what information gap the query is trying to fill.
    """
    prompt = (
        f"Given the following prompt, generate {n} unique search queries to "
        f"research the topic thoroughly. For each query, provide a research goal. "
        f"Format as 'Query: <query>' followed by 'Goal: <goal>' for each pair: "
        f"{task}"
    )

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are an expert researcher generating search queries."
            },
            {"role": "user", "content": prompt}
        ],
        temperature=0.4
    )

    lines = resp.choices[0].message.content.strip().split("\n")
    queries = []
    current = {}

    for line in lines:
        line = line.strip()
        if line.startswith("Query:"):
            if current:
                queries.append(current)
            current = {"query": line.replace("Query:", "").strip()}
        elif line.startswith("Goal:") and current:
            current["researchGoal"] = line.replace("Goal:", "").strip()

    if current:
        queries.append(current)

    return queries[:n]


def header(title: str, source: str):
    print(f"\n{'═' * 64}")
    print(f"  {title}")
    print(f"  source: {source}")
    print(f"{'═' * 64}")


def run():
    print(f"\nTask:\n  {TASK}\n")
    print(f"Generating {N_QUERIES} sub-queries per approach...\n")

    # GPT-Researcher
    header(
        "1. GPT-Researcher",
        "gpt_researcher/prompts/__init__.py → generate_search_queries_prompt()"
    )
    gptr = gpt_researcher_decompose(TASK)
    for i, q in enumerate(gptr, 1):
        print(f"  {i}. {q}")

    # Deep Research
    header(
        "2. Deep Research",
        "gpt_researcher/skills/deep_research.py → generate_search_queries()"
    )
    dr = deep_research_decompose(TASK)
    for i, item in enumerate(dr, 1):
        print(f"\n  {i}. Query: {item.get('query', '')}")
        print(f"     Goal:  {item.get('researchGoal', '')}")

    # Comparison
    print(f"\n{'─' * 64}")
    print("WHAT EACH PRODUCES")
    print(f"{'─' * 64}")
    print()
    print("  GPT-Researcher:")
    print("    - Short keyword strings, designed for Google search")
    print("    - No explanation of why each query was generated")
    print("    - Good raw input for Valyu search API")
    print()
    print("  Deep Research:")
    print("    - Query + research goal per sub-query")
    print("    - Goal field explains what information gap each query fills")
    print("    - Closer to bootk.ai: query + intent in one call")
    print()
    print("  For bootk.ai prototype:")
    print("    → Use Deep Research format (query + goal)")
    print("    → Goal maps to intent signal → informs price ceiling")
    print("    → Query string goes directly into Valyu search API")
    print(f"{'─' * 64}\n")


if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY first:  export OPENAI_API_KEY=sk-...")
    else:
        run()
