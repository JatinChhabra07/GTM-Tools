"""product.yaml schema (plan §7). The /gtm-setup wizard writes it; nothing else should need to."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from gtm.paths import check_slug, product_config_dir

Seniority = Literal["c_level", "vp", "director", "manager", "ic"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductInfo(Strict):
    slug: str
    name: str
    url: HttpUrl
    mcp: str | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        return check_slug(v)


class Segment(Strict):
    name: str
    industries: list[str] = []
    size_band: list[str] = []
    geos: list[str] = []
    tech_stack_any: list[str] = []


class Persona(Strict):
    name: str
    titles: list[str] = Field(min_length=1)  # literal business-card titles: matched by substring
    seniority: Seniority


class GeoRule(Strict):
    status: Literal["allowed", "allowed_with_notice", "blocked"]
    legal_signoff: str | None = None  # who/when the founder confirmed legal advice (India)
    notice: str | None = None


class Icp(Strict):
    segments: list[Segment] = []


class SignalInputs(Strict):
    linkedin: dict = {}
    x: dict = {}
    instagram: dict = {"enabled": False}
    pain_language: dict = {}
    hiring_keywords: list[str] = []
    competitors: list[str] = []


class Tal(Strict):
    refresh: Literal["weekly", "monthly"] = "weekly"
    max_accounts: int = 500
    build_budget_usd: float = 10
    max_items_per_query: int = 200
    profile_refresh_days: int = 30


class Scoring(Strict):
    weights: dict[str, int | str] = {"icp_fit": 30, "intent": 30, "lookalike": "auto",
                                     "authority": 10, "contactability": 10}
    prescore_cutoff: float = 60
    cutoff: float = 70
    recontact_days: int = 90
    allow_catchall: bool = False
    holdout_pct: int = Field(20, ge=0, le=50)


# Claude plan profiles (plan §8): built on Max 20x, must work on Max 5x.
USAGE_PROFILES = {
    "max5x": {"llm_calls_per_run": 60, "qualify_reviewers": 2},
    "max20x": {"llm_calls_per_run": 150, "qualify_reviewers": 4},
}


class Usage(Strict):
    profile: Literal["max5x", "max20x"] = "max5x"  # safe default: the smaller plan
    usage_limit_pause_minutes: int = 60

    def limits(self) -> dict:
        return USAGE_PROFILES[self.profile]


class Budgets(Strict):
    apify_usd_per_run: float = 3
    snov_credits_per_run: int = 60
    max_items_per_actor: int = 100


class RunSlot(Strict):
    at: str
    mode: Literal["full", "refresh"]


class Schedule(Strict):
    runs: list[RunSlot] = [RunSlot(at="09:00", mode="full"), RunSlot(at="15:00", mode="refresh")]
    sync: Literal["hourly"] = "hourly"
    feedback: Literal["daily"] = "daily"
    timezone: str = "Asia/Kolkata"


class MailboxShare(Strict):
    id: str
    share: float = Field(1.0, gt=0, le=1)


class Sending(Strict):
    auto_send_min_score: float = 85  # enabling auto-send is NOT a config option (plan §6.3)
    snov_list: str | None = None
    mailboxes: list[MailboxShare] = []
    touches: int = 3
    followup_days: list[int] = [4, 12]
    window: dict = {"start": "09:00", "end": "18:00", "tz": "recipient"}


class Voice(Strict):
    tone: str = "direct, peer-to-peer"
    banned_extra: list[str] = []
    sender_name: str = ""
    postal_address: str = ""
    opt_out_line: str = 'Not interested? Reply "no thanks" and I won\'t email again.'


DEFAULT_GEO = {
    "US": GeoRule(status="allowed"),
    "IN": GeoRule(status="blocked"),  # unblocked only after the founder confirms legal advice
    "EU": GeoRule(status="blocked"),
    "UK": GeoRule(status="blocked"),
    "default": GeoRule(status="blocked"),
}


class ProductConfig(Strict):
    product: ProductInfo
    icp: Icp = Icp()
    anti_icp: list[str] = []
    personas: list[Persona] = []
    geo_policy: dict[str, GeoRule] = DEFAULT_GEO
    signals: SignalInputs = SignalInputs()
    tal: Tal = Tal()
    scoring: Scoring = Scoring()
    usage: Usage = Usage()
    budgets: Budgets = Budgets()
    schedule: Schedule = Schedule()
    sending: Sending = Sending()
    voice: Voice = Voice()

    @field_validator("geo_policy")
    @classmethod
    def _geo(cls, v: dict[str, GeoRule]) -> dict[str, GeoRule]:
        if "default" not in v:
            raise ValueError("geo_policy needs a 'default' entry")
        for code, rule in v.items():
            if rule.status == "allowed_with_notice" and not rule.notice:
                raise ValueError(f"geo_policy.{code}: allowed_with_notice needs notice text")
        india = v.get("IN")
        if india and india.status != "blocked" and not india.legal_signoff:
            raise ValueError("geo_policy.IN can only be unblocked with legal_signoff set")
        return v


def config_path(slug: str) -> Path:
    return product_config_dir(slug) / "product.yaml"


def load(slug: str) -> ProductConfig:
    path = config_path(slug)
    if not path.exists():
        raise FileNotFoundError(f"no product.yaml for {slug!r}: run /gtm-setup")
    cfg = ProductConfig.model_validate(yaml.safe_load(path.read_text()) or {})
    if cfg.product.slug != slug:
        raise ValueError(f"{path} says slug {cfg.product.slug!r}, expected {slug!r}")
    return cfg


def save(cfg: ProductConfig) -> Path:
    path = config_path(cfg.product.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json", exclude_none=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return path
