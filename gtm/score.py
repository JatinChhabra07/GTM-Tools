"""Lead score 0-100 (plan §4.2). Deterministic; isolated reviewers can only lower or drop it.

Dimensions: ICP fit, intent (triggers capped at 40% of it), lookalike (weight scales with how many
clients we learned from; the rest moves to intent), authority, contactability.
Pre-score (accounts, before any paid call) = ICP fit + lookalike, normalised to 0-100.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from gtm.config import ProductConfig
from gtm.gates import country_code
from gtm.observations import Latest, latest

SENIORITY_VALUE = {"c_level": 1.0, "vp": 0.9, "director": 0.75, "manager": 0.5, "ic": 0.25}
TRIGGER_SHARE = 0.4


def lookalike_weight(n_clients: int) -> int:
    if n_clients < 10:
        return 5
    if n_clients <= 30:
        return 10
    return 20


def weights(cfg: ProductConfig, n_clients: int) -> dict[str, float]:
    w = cfg.scoring.weights
    icp, intent = float(w.get("icp_fit", 30)), float(w.get("intent", 30))
    auth, contact = float(w.get("authority", 10)), float(w.get("contactability", 10))
    look_nominal = 100 - icp - intent - auth - contact  # 20 with the defaults
    look = w.get("lookalike", "auto")
    look = float(lookalike_weight(n_clients)) if look == "auto" else float(look)
    look = min(look, look_nominal) if look_nominal > 0 else look
    return {"icp_fit": icp, "intent": intent + max(0.0, look_nominal - look), "lookalike": look,
            "authority": auth, "contactability": contact}


# ---------------------------------------------------------------- firmographics

@dataclass
class Firmo:
    industry: str = ""
    size_band: str = ""
    country: str | None = None
    tech: set[str] = field(default_factory=set)


def firmographics(account: sqlite3.Row | dict, obs: list[Latest]) -> Firmo:
    f = Firmo(industry=(account["industry"] or "").lower(), size_band=account["size_band"] or "",
              country=country_code(account["hq_country"]))
    for o in obs:
        if o.signal_code == "INDUSTRY" and not f.industry:
            f.industry = (o.value_text or "").lower()
        elif o.signal_code == "SIZE_BAND" and not f.size_band:
            f.size_band = o.value_text or ""
        elif o.signal_code == "HQ_COUNTRY" and not f.country:
            f.country = country_code(o.value_text)
        elif o.signal_code == "TECH_STACK" and o.value_text:
            f.tech.add(o.value_text.lower())
    return f


def _band_bounds(band: str) -> tuple[int, int] | None:
    b = band.replace(" ", "").replace(",", "")
    if b.endswith("+"):
        return int(b[:-1]), 10**9
    if "-" in b:
        lo, hi = b.split("-", 1)
        if lo.isdigit() and hi.isdigit():
            return int(lo), int(hi)
    return None


def bands_overlap(a: str, b: str) -> bool:
    x, y = _band_bounds(a), _band_bounds(b)
    if not x or not y:
        return a.strip().lower() == b.strip().lower()
    return x[0] <= y[1] and y[0] <= x[1]


def _industry_match(industry: str, wanted: list[str]) -> bool:
    return any(w.lower() in industry or (industry and industry in w.lower()) for w in wanted)


def icp_fit(cfg: ProductConfig, f: Firmo) -> tuple[float, str | None]:
    """Best segment's share of satisfied criteria (0-1). Missing data counts as not satisfied."""
    best, best_name = 0.0, None
    for seg in cfg.icp.segments:
        checks = []
        if seg.industries:
            checks.append(_industry_match(f.industry, seg.industries))
        if seg.size_band:
            checks.append(bool(f.size_band) and any(bands_overlap(f.size_band, b) for b in seg.size_band))
        if seg.geos:
            checks.append(bool(f.country) and f.country in {country_code(g) or g for g in seg.geos})
        if seg.tech_stack_any:
            checks.append(any(t.lower() in f.tech for t in seg.tech_stack_any))
        frac = sum(checks) / len(checks) if checks else 0.0
        if frac > best:
            best, best_name = frac, seg.name
    return best, best_name


# ---------------------------------------------------------------- lookalike (clients fingerprint)

@dataclass
class ClientPrint:
    domain: str
    firmo: Firmo
    weight: float


def client_prints(conn: sqlite3.Connection) -> list[ClientPrint]:
    rows = conn.execute("SELECT c.domain, c.contract_size, c.retention_months, a.industry, a.size_band, "
                        "a.hq_country FROM clients c LEFT JOIN accounts a ON a.domain = c.domain").fetchall()
    sizes = [r["contract_size"] for r in rows if r["contract_size"]]
    median = sorted(sizes)[len(sizes) // 2] if sizes else None
    out = []
    for r in rows:
        acct = {"industry": r["industry"], "size_band": r["size_band"], "hq_country": r["hq_country"]}
        f = firmographics(acct, latest(conn, r["domain"], kinds=("firmographic",)))
        w = 1.0
        if median and r["contract_size"]:
            w *= min(2.0, max(0.5, r["contract_size"] / median))
        if r["retention_months"]:
            w *= min(1.5, max(0.75, r["retention_months"] / 12))
        out.append(ClientPrint(r["domain"], f, w))
    return out


def similarity(a: Firmo, b: Firmo) -> float:
    parts = []
    if a.industry and b.industry:
        parts.append((0.35, 1.0 if (a.industry in b.industry or b.industry in a.industry) else 0.0))
    if a.size_band and b.size_band:
        parts.append((0.2, 1.0 if bands_overlap(a.size_band, b.size_band) else 0.0))
    if a.country and b.country:
        parts.append((0.15, 1.0 if a.country == b.country else 0.0))
    if a.tech and b.tech:
        parts.append((0.3, len(a.tech & b.tech) / len(a.tech | b.tech)))
    if not parts:
        return 0.0
    # unknown dimensions shrink toward 0 instead of being ignored: thin data shouldn't look similar
    known = sum(w for w, _ in parts)
    return sum(w * v for w, v in parts) / known * min(1.0, known / 0.7)


def lookalike(f: Firmo, prints: list[ClientPrint], exclude: str | None = None, k: int = 3) -> float:
    sims = sorted(((similarity(f, p.firmo), p.weight) for p in prints if p.domain != exclude), reverse=True)[:k]
    if not sims:
        return 0.0
    return sum(s * w for s, w in sims) / sum(w for _, w in sims)


# ---------------------------------------------------------------- intent

def _decay(o: Latest) -> float:
    if o.freshness_days is None:
        return 1.0
    if not o.fresh:
        return 0.0
    return max(0.3, 1.0 - o.age_days / o.freshness_days * 0.7)


def intent_frac(obs: list[Latest]) -> tuple[float, list[int]]:
    """(0-1, observation ids used). Stacking capped: best + 20% of the second."""
    intents = sorted((((o.value_num or 2) / 3) * _decay(o), o.id) for o in obs if o.kind == "intent")
    triggers = sorted(((1.0 if o.rank <= 5 else 0.7 if o.rank <= 7 else 0.5) * _decay(o), o.id)
                      for o in obs if o.kind == "trigger")
    intents.reverse()
    triggers.reverse()

    def stack(xs):
        if not xs:
            return 0.0
        return min(1.0, xs[0][0] + (0.2 * xs[1][0] if len(xs) > 1 else 0.0))

    used = [i for _, i in intents[:2] + triggers[:2] if _ > 0]
    return min(1.0, stack(intents) + TRIGGER_SHARE * stack(triggers)), used


# ---------------------------------------------------------------- totals

def contactability(lead: sqlite3.Row | dict) -> float:
    cls = lead["email_class"]
    base = {"valid": 0.7, "catchall": 0.4}.get(cls or "", 0.0)
    if lead["is_generic_inbox"]:
        base = min(base, 0.3)
    return min(1.0, base + (0.3 if lead["is_signal_author"] else 0.0))


@dataclass
class ScoreResult:
    total: float
    dims: dict[str, float]
    weights: dict[str, float]
    segment: str | None
    observation_ids: list[int]


def prescore(cfg: ProductConfig, f: Firmo, look: float, n_clients: int) -> float:
    w = weights(cfg, n_clients)
    fit, _ = icp_fit(cfg, f)
    denom = w["icp_fit"] + w["lookalike"]
    return round(100 * (fit * w["icp_fit"] + look * w["lookalike"]) / denom, 1) if denom else 0.0


def score_lead(cfg: ProductConfig, *, account: sqlite3.Row, lead: sqlite3.Row, obs: list[Latest],
               n_clients: int) -> ScoreResult:
    w = weights(cfg, n_clients)
    f = firmographics(account, obs)
    fit, seg = icp_fit(cfg, f)
    intent, used = intent_frac(obs)
    dims = {
        "icp_fit": fit * w["icp_fit"],
        "intent": intent * w["intent"],
        "lookalike": float(account["lookalike_score"] or 0) * w["lookalike"],
        "authority": SENIORITY_VALUE.get(lead["seniority"] or "", 0.0) * w["authority"],
        "contactability": contactability(lead) * w["contactability"],
    }
    total = round(min(100.0, sum(dims.values())), 1)
    return ScoreResult(total, {k: round(v, 2) for k, v in dims.items()}, w, seg, used)
