"""
Learning system: persist conversion events and aggregate publisher value by query type.
Learn which publishers deliver value for which query types (citation rate, quality, cost).
"""

import hashlib
import json
import os
import random
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# K-anonymity: only report aggregates when at least this many events per (cluster, publisher)
MIN_SAMPLE_SIZE = 5
# Differential privacy: scale of Laplace noise (higher = more privacy, noisier)
DP_EPSILON = 1.0
DP_SENSITIVITY = 0.1


@dataclass
class ConversionEvent:
    """
    Event log for a single purchase/optimization decision.
    Stored for learning; outcomes can be updated later via feedback.
    """
    # Required identifiers and context
    event_id: str
    query_id: str
    query_text: str

    # Optional / with defaults (all must follow required fields)
    customer_id: str = "default"  # Hashed for multi-tenant privacy
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    query_hash: str = ""
    query_cluster: str = ""  # intent, e.g. "financial_analysis", "breaking_news"
    intent: str = ""

    # Purchase decision (from optimizer)
    sources_purchased: List[str] = field(default_factory=list)
    total_cost: float = 0.0
    decision_confidence: float = 0.0

    # Tier 1: Direct usage (filled by feedback)
    sources_cited: List[str] = field(default_factory=list)
    citation_rate: float = 0.0
    utilization_by_source: Dict[str, float] = field(default_factory=dict)

    # Tier 2: Quality / satisfaction (filled by feedback)
    answer_quality: Optional[float] = None
    user_rating: Optional[float] = None
    correction_made: bool = False

    # Derived
    cost_efficiency: Optional[float] = None  # quality / cost when available

    def __post_init__(self):
        if not self.query_hash and self.query_text:
            self.query_hash = hashlib.sha256(self.query_text.encode()).hexdigest()[:16]
        if not self.query_cluster and self.intent:
            self.query_cluster = self.intent

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ConversionEvent":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _laplace_noise(scale: float) -> float:
    """Add Laplace noise for differential privacy."""
    u = random.random() - 0.5
    return -scale * (1.0 if u < 0 else -1.0) * (__import__("math").log(1 - 2 * abs(u)))


class MetricsStore:
    """
    Persist conversion events and maintain global aggregates by (query_cluster, publisher).
    Privacy: k-anonymity (only report when N >= MIN_SAMPLE_SIZE), optional DP noise.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.environ.get("LEARNING_DB", "learning.db")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS conversion_events (
                    event_id TEXT PRIMARY KEY,
                    query_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    query_cluster TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    sources_purchased TEXT NOT NULL,
                    total_cost REAL NOT NULL,
                    decision_confidence REAL NOT NULL,
                    sources_cited TEXT NOT NULL,
                    citation_rate REAL NOT NULL,
                    utilization_by_source TEXT NOT NULL,
                    answer_quality REAL,
                    user_rating REAL,
                    correction_made INTEGER NOT NULL DEFAULT 0,
                    cost_efficiency REAL
                );
                CREATE INDEX IF NOT EXISTS idx_events_cluster ON conversion_events(query_cluster);
                CREATE INDEX IF NOT EXISTS idx_events_timestamp ON conversion_events(timestamp);
                CREATE INDEX IF NOT EXISTS idx_events_customer ON conversion_events(customer_id);

                CREATE TABLE IF NOT EXISTS global_aggregates (
                    query_cluster TEXT NOT NULL,
                    publisher TEXT NOT NULL,
                    total_purchases INTEGER NOT NULL DEFAULT 0,
                    total_citations INTEGER NOT NULL DEFAULT 0,
                    sum_quality REAL NOT NULL DEFAULT 0,
                    sum_cost REAL NOT NULL DEFAULT 0,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (query_cluster, publisher)
                );
            """)

    def log_event(self, event: ConversionEvent) -> None:
        """Store one conversion event and update global aggregates."""
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO conversion_events (
                    event_id, query_id, customer_id, timestamp,
                    query_text, query_hash, query_cluster, intent,
                    sources_purchased, total_cost, decision_confidence,
                    sources_cited, citation_rate, utilization_by_source,
                    answer_quality, user_rating, correction_made, cost_efficiency
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.event_id,
                event.query_id,
                event.customer_id,
                event.timestamp,
                event.query_text,
                event.query_hash,
                event.query_cluster,
                event.intent,
                json.dumps(event.sources_purchased),
                event.total_cost,
                event.decision_confidence,
                json.dumps(event.sources_cited),
                event.citation_rate,
                json.dumps(event.utilization_by_source),
                event.answer_quality,
                event.user_rating,
                1 if event.correction_made else 0,
                event.cost_efficiency,
            ))
            self._update_global_aggregates(c, event)

    def _update_global_aggregates(self, c: sqlite3.Connection, event: ConversionEvent) -> None:
        """Update per-(cluster, publisher) aggregates. Quality/citations only when we have feedback."""
        cluster = event.query_cluster or event.intent
        # At log time we usually have no outcomes; quality/citations are added in submit_feedback
        quality = None
        if event.answer_quality is not None:
            quality = event.answer_quality + _laplace_noise(DP_SENSITIVITY / DP_EPSILON)

        for pub in event.sources_purchased:
            cited = 1 if pub in event.sources_cited else 0
            if quality is not None:
                c.execute("""
                    INSERT INTO global_aggregates (query_cluster, publisher, total_purchases, total_citations, sum_quality, sum_cost, count)
                    VALUES (?, ?, 1, ?, ?, ?, 1)
                    ON CONFLICT(query_cluster, publisher) DO UPDATE SET
                        total_purchases = total_purchases + 1,
                        total_citations = total_citations + ?,
                        sum_quality = sum_quality + ?,
                        sum_cost = sum_cost + ?,
                        count = count + 1
                """, (cluster, pub, cited, quality, event.total_cost, cited, quality, event.total_cost))
            else:
                c.execute("""
                    INSERT INTO global_aggregates (query_cluster, publisher, total_purchases, total_citations, sum_quality, sum_cost, count)
                    VALUES (?, ?, 1, 0, 0, ?, 1)
                    ON CONFLICT(query_cluster, publisher) DO UPDATE SET
                        total_purchases = total_purchases + 1,
                        sum_cost = sum_cost + ?,
                        count = count + 1
                """, (cluster, pub, event.total_cost, event.total_cost))

    def submit_feedback(
        self,
        event_id: str,
        sources_cited: List[str],
        answer_quality: Optional[float] = None,
        user_rating: Optional[float] = None,
        correction_made: bool = False,
    ) -> bool:
        """Update an existing event with outcome feedback; recomputes aggregates for that event."""
        with self._conn() as c:
            row = c.execute(
                "SELECT query_cluster, intent, sources_purchased, total_cost FROM conversion_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if not row:
                return False
            cluster, intent, purchased_json, total_cost = row
            purchased = json.loads(purchased_json)
            citation_rate = len(sources_cited) / len(purchased) if purchased else 0.0
            quality = answer_quality if answer_quality is not None else user_rating
            cost_eff = (quality / total_cost) if (quality is not None and total_cost > 0) else None

            c.execute("""
                UPDATE conversion_events SET
                    sources_cited = ?, citation_rate = ?, answer_quality = ?, user_rating = ?, correction_made = ?, cost_efficiency = ?
                WHERE event_id = ?
            """, (json.dumps(sources_cited), citation_rate, answer_quality, user_rating, 1 if correction_made else 0, cost_eff, event_id))

            # Re-aggregate: we'd need to subtract old contribution and add new. For simplicity, we don't back-propagate
            # old aggregates; we only add the new outcome. So we update aggregates with the delta in quality/citations.
            # Actually the event was already counted in aggregates at log time with 0 quality/citations. So we need to
            # add the delta: +quality, +citations for each cited source. Easiest: run a one-off aggregate update
            # for this event only (add quality and citations as if we're now seeing the outcome).
            for pub in purchased:
                cited = 1 if pub in sources_cited else 0
                if quality is not None:
                    q = quality + _laplace_noise(DP_SENSITIVITY / DP_EPSILON)
                    c.execute("""
                        UPDATE global_aggregates SET
                            total_citations = total_citations + ?,
                            sum_quality = sum_quality + ?
                        WHERE query_cluster = ? AND publisher = ?
                    """, (cited, q, cluster, pub))
                else:
                    c.execute("""
                        UPDATE global_aggregates SET
                            total_citations = total_citations + ?
                        WHERE query_cluster = ? AND publisher = ?
                    """, (cited, cluster, pub))
        return True

    def get_global_publisher_performance(
        self,
        query_cluster: Optional[str] = None,
        min_sample_size: int = MIN_SAMPLE_SIZE,
    ) -> Dict[str, Any]:
        """
        Return learned publisher performance by cluster.
        Only includes (cluster, publisher) with count >= min_sample_size (k-anonymity).
        """
        with self._conn() as c:
            if query_cluster:
                rows = c.execute("""
                    SELECT query_cluster, publisher, total_purchases, total_citations, sum_quality, sum_cost, count
                    FROM global_aggregates WHERE query_cluster = ? AND count >= ?
                """, (query_cluster, min_sample_size)).fetchall()
            else:
                rows = c.execute("""
                    SELECT query_cluster, publisher, total_purchases, total_citations, sum_quality, sum_cost, count
                    FROM global_aggregates WHERE count >= ?
                """, (min_sample_size,)).fetchall()

        by_cluster: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for cluster, publisher, purchases, citations, sum_q, sum_cost, count in rows:
            if cluster not in by_cluster:
                by_cluster[cluster] = {}
            avg_cost = sum_cost / count if count else 0
            avg_quality = sum_q / count if count else 0
            value_per_dollar = avg_quality / avg_cost if (count and avg_cost > 0) else 0
            by_cluster[cluster][publisher] = {
                "purchase_count": purchases,
                "citation_count": citations,
                "citation_rate": citations / purchases if purchases else 0,
                "avg_quality": avg_quality,
                "avg_cost": avg_cost,
                "value_per_dollar": value_per_dollar,
                "sample_size": count,
            }
        return {"by_cluster": by_cluster, "min_sample_size": min_sample_size}

    def get_learned_domain_boost(self, query_cluster: str) -> Dict[str, float]:
        """
        Return a boost map (publisher -> boost in [0, 1]) for the given cluster,
        derived from citation_rate and value_per_dollar. Use to blend with static DOMAIN_BOOST.
        """
        perf = self.get_global_publisher_performance(query_cluster=query_cluster)
        cluster_data = perf.get("by_cluster", {}).get(query_cluster, {})
        boost = {}
        for pub, stats in cluster_data.items():
            rate = stats["citation_rate"]
            vpd = stats.get("value_per_dollar") or 0
            # Normalize to roughly 0–0.4 range so we can add to static boosts
            boost[pub] = min(0.4, rate * 0.3 + (min(vpd, 2.0) / 2.0) * 0.2)
        return boost

    def event_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM conversion_events").fetchone()[0]


# Singleton store for the app
_store: Optional[MetricsStore] = None


def get_metrics_store() -> MetricsStore:
    global _store
    if _store is None:
        _store = MetricsStore()
    return _store
