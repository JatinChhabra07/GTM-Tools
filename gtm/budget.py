"""Per-run spending caps (plan §4.2, §5 Phase 3). Exceeding one ends the run `partial`, never retried.

Apify spend is enforced twice: each actor run carries its own `max_total_charge_usd`, sized from
what's left here, and the run stops starting new actors once the remaining budget is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    pass


@dataclass
class Budget:
    apify_usd: float
    snov_credits: int
    spent_apify_usd: float = 0.0
    spent_snov_credits: int = 0
    lines: list[dict] = field(default_factory=list)

    @property
    def apify_left(self) -> float:
        return max(0.0, self.apify_usd - self.spent_apify_usd)

    @property
    def snov_left(self) -> int:
        return max(0, self.snov_credits - self.spent_snov_credits)

    def reserve_apify(self, estimate_usd: float, what: str) -> float:
        """The charge cap for the next actor run: never more than what's left."""
        if self.apify_left <= 0.005:
            raise BudgetExceeded(f"Apify budget ${self.apify_usd:.2f} used up before {what}")
        return min(self.apify_left, max(estimate_usd, 0.01))

    def charge_apify(self, usd: float, what: str, run_id: str | None = None) -> None:
        self.spent_apify_usd += usd
        self.lines.append({"tool": "apify", "what": what, "usd": round(usd, 4), "run_id": run_id})

    def reserve_snov(self, credits: int, what: str) -> None:
        if credits > self.snov_left:
            raise BudgetExceeded(f"Snov budget {self.snov_credits} credits used up before {what}")

    def charge_snov(self, credits: int, what: str) -> None:
        self.spent_snov_credits += credits
        self.lines.append({"tool": "snov", "what": what, "credits": credits})

    def summary(self) -> dict:
        return {"apify_usd": round(self.spent_apify_usd, 4), "apify_cap_usd": self.apify_usd,
                "snov_credits": self.spent_snov_credits, "snov_cap": self.snov_credits,
                "lines": self.lines}
