"""Starter B2B signal catalog (plan §5.1). Seeded into each product's signal_codes table.

Non-system codes start as `proposed`: nothing is collected for them until the founder confirms
them in /gtm-icp. `contact` and `health` codes are system codes: always confirmed, never
rejectable, so failures can't be silenced.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Code:
    code: str
    kind: str
    meaning: str
    value_type: str = "bool"
    cost_class: str = "free"
    cost_note: str = ""
    freshness_days: int | None = None
    rank: int = 100
    min_confidence: float = 0.5
    threshold_op: str | None = None
    threshold_value: float | None = None

    @property
    def system(self) -> bool:
        return self.kind in ("contact", "health")


CATALOG: tuple[Code, ...] = (
    # intent: someone at the account says they have the problem (rank = opener priority)
    Code("ASKS_FOR_SOLUTION", "intent", "A named person asks for a tool/vendor in our category",
         cost_class="paid", cost_note="Apify LinkedIn/X post search", freshness_days=14, rank=1),
    Code("COMPETITOR_PAIN", "intent", "A named person complains about a competitor or the pain we solve",
         cost_class="paid", cost_note="Apify LinkedIn/X post search", freshness_days=21, rank=2),
    Code("SWITCHING", "intent", "A named person says they are moving off their current tool",
         cost_class="paid", cost_note="Apify LinkedIn/X post search", freshness_days=21, rank=2),
    Code("HIRING_FOR_PROBLEM", "intent", "Job post for a role whose work our product does or helps",
         cost_class="paid", cost_note="Apify LinkedIn jobs (also free from their careers page)",
         freshness_days=45, rank=3),
    # triggers: company events that make buying likely
    Code("HIRING_ROLE", "trigger", "Company is hiring a role in our buyer's team", value_type="text",
         cost_class="paid", cost_note="Apify LinkedIn jobs (also free from their careers page)",
         freshness_days=45, rank=4),
    Code("FUNDING", "trigger", "Company raised money", value_type="text", freshness_days=60, rank=5),
    Code("LEADERSHIP_CHANGE", "trigger", "New executive in our buyer's function", value_type="text",
         freshness_days=90, rank=5),
    Code("LAUNCH", "trigger", "Company launched a product or market", value_type="text",
         freshness_days=45, rank=6),
    Code("EXPANSION", "trigger", "New office, region or team", value_type="text",
         freshness_days=60, rank=6),
    Code("SITE_CHANGE", "trigger", "Meaningful website change (wigolo diff)", value_type="text",
         freshness_days=30, rank=8),
    Code("PRICING_CHANGE", "trigger", "Pricing page changed", value_type="text", freshness_days=30, rank=7),
    Code("TECH_ADDED", "trigger", "A relevant tool appeared on their site", value_type="text",
         freshness_days=30, rank=7),
    Code("TECH_REMOVED", "trigger", "A relevant tool disappeared from their site", value_type="text",
         freshness_days=30, rank=7),
    Code("RUNNING_GOOGLE_ADS", "trigger", "Google Ads tag on their site (spending on acquisition now)",
         freshness_days=30, rank=9),
    Code("RUNNING_META_ADS", "trigger", "Meta pixel on their site", freshness_days=30, rank=9),
    # firmographic: who the account is
    Code("INDUSTRY", "firmographic", "Industry", value_type="text", rank=20),
    Code("SIZE_BAND", "firmographic", "Employee band", value_type="text", rank=20),
    Code("HQ_COUNTRY", "firmographic", "HQ country (ISO code)", value_type="text", rank=20),
    Code("TECH_STACK", "firmographic", "A tool they use", value_type="text", rank=15),
    Code("SITE_PAGE_COUNT", "firmographic", "Pages in sitemap (size proxy)", value_type="num", rank=30,
         threshold_op=">=", threshold_value=20),
    # contact (system): who to email and whether we can
    Code("TEAM_MEMBER_FOUND", "contact", "A named person with a title on their team page",
         value_type="text", rank=50),
    Code("DECISION_MAKER_TITLE", "contact", "A person matching one of our personas", value_type="text", rank=50),
    Code("EMAIL_PATTERN", "contact", "Company email pattern learned", value_type="text", rank=60),
    Code("EMAIL_STATUS", "contact", "Verification status of a lead's email", value_type="text",
         cost_class="paid", cost_note="Snov verify credit", rank=60),
    Code("GENERIC_INBOX", "contact", "A general inbox printed on their own site", value_type="text", rank=70),
    # health (system): failures are observations, never silence
    Code("SITE_UNREACHABLE", "health", "Their website could not be fetched", value_type="text", rank=90),
    Code("PROFILE_FAILED", "health", "Profiling the site failed part-way", value_type="text", rank=90),
    Code("SOURCE_THROTTLED", "health", "A data source rate-limited or refused us", value_type="text", rank=90),
)


def seed(conn: sqlite3.Connection) -> int:
    """Insert missing codes. Never overrides a status the founder already chose (from Harvey)."""
    added = 0
    for c in CATALOG:
        cur = conn.execute(
            """INSERT OR IGNORE INTO signal_codes
               (code, status, kind, system, cost_class, cost_note, meaning, value_type,
                threshold_op, threshold_value, min_confidence, freshness_days, rank)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c.code, "confirmed" if c.system else "proposed", c.kind, int(c.system), c.cost_class,
             c.cost_note, c.meaning, c.value_type, c.threshold_op, c.threshold_value,
             c.min_confidence, c.freshness_days, c.rank),
        )
        added += cur.rowcount
    return added
