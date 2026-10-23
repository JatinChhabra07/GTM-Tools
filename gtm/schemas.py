"""Output schemas for isolated calls (one per prompt_id in prompts/). The model's JSON is
validated against these; anything that doesn't validate is retried, then marked needs_human."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- collection

class PostLabel(Out):
    post_id: str
    code: str | None = None               # one confirmed intent code, or null
    strength: int | None = Field(None, ge=1, le=3)
    employer: str | None = None           # ONLY if the post/author text states it
    drop_reason: str | None = None        # vendor | job_seeker | meme | off_topic | unclear


class PostLabels(Out):
    labels: list[PostLabel]


class PainPhrases(Out):
    phrases: list[str] = Field(default_factory=list, max_length=15)


class Candidate(Out):
    url: str
    domain: str | None = None
    name: str | None = None
    is_company_site: bool = False
    is_company_list: bool = False  # a page naming many companies (top-N list, directory): mined next
    reason: str = ""


class Candidates(Out):
    companies: list[Candidate]


class EmployerResolution(Out):
    employer: str
    domain: str | None = None
    evidence_url: str | None = None
    confidence: float = Field(0, ge=0, le=1)


class EmployerResolutions(Out):
    resolutions: list[EmployerResolution]


class NewsEvent(Out):
    domain: str
    code: str | None = None               # FUNDING | LEADERSHIP_CHANGE | LAUNCH | EXPANSION | null
    summary: str = ""
    evidence_url: str | None = None


class NewsEvents(Out):
    events: list[NewsEvent]


class PersonaMatch(Out):
    title: str
    persona: str | None = None


class PersonaMatches(Out):
    matches: list[PersonaMatch]


# ---------------------------------------------------------------- competitors (plan §15.1 E2)

class CompetitorClaim(Out):
    kind: Literal["positioning", "serves", "feature", "pricing", "complaint", "praise"]
    text: str
    source_url: str


class CompetitorBrief(Out):
    claims: list[CompetitorClaim] = Field(default_factory=list)


class CustomerMention(Out):
    entry: int                            # the ENTRY number in the page block
    name: str = ""
    is_company: bool = False
    relation: Literal["customer", "partner", "integration", "investor", "press", "other"] = "other"


class CustomerMentions(Out):
    mentions: list[CustomerMention]


class CompetitorPostLabel(Out):
    post_id: str
    competitor: str | None = None         # one of the input competitors, named in the post
    relation: Literal["uses", "leaving", "complains"] | None = None
    employer: str | None = None           # ONLY if the post/author text states it
    drop_reason: str | None = None


class CompetitorPostLabels(Out):
    labels: list[CompetitorPostLabel]


class ReviewTheme(Out):
    kind: Literal["complaint", "praise"]
    text: str
    review_id: str


class ReviewRead(Out):
    review_id: str
    employer: str | None = None           # ONLY when the review text names the reviewer's own company


class ReviewReading(Out):
    themes: list[ReviewTheme] = Field(default_factory=list)
    reviews: list[ReviewRead] = Field(default_factory=list)


# ---------------------------------------------------------------- qualification graph

class WorkerLead(Out):
    lead_key: str
    assessment: str
    concerns: list[str] = Field(default_factory=list)
    penalty: int = Field(0, ge=-40, le=0)


class WorkerOut(Out):
    leads: list[WorkerLead]


class Finding(Out):
    lead_key: str
    verdict: Literal["ok", "lower", "drop"]
    penalty: int = Field(0, ge=-40, le=0)
    reason: str = ""


class ReviewOut(Out):
    findings: list[Finding]


class Verdict(Out):
    lead_key: str
    verdict: Literal["keep", "drop", "needs_rework"]
    penalty: int = Field(0, ge=-40, le=0)
    reason: str = ""


class SynthOut(Out):
    verdicts: list[Verdict]


# ---------------------------------------------------------------- account shortlist (before paying)

class AccountAssessment(Out):
    domain: str
    assessment: str = ""
    concerns: list[str] = Field(default_factory=list)
    penalty: int = Field(0, ge=-40, le=0)


class ShortlistWorkerOut(Out):
    accounts: list[AccountAssessment]


class AccountFinding(Out):
    domain: str
    verdict: Literal["ok", "lower", "drop"]
    penalty: int = Field(0, ge=-40, le=0)
    reason: str = ""


class ShortlistReviewOut(Out):
    findings: list[AccountFinding]


class AccountVerdict(Out):
    domain: str
    verdict: Literal["keep", "drop"]
    penalty: int = Field(0, ge=-40, le=0)
    reason: str = ""


class ShortlistSynthOut(Out):
    verdicts: list[AccountVerdict]


# ---------------------------------------------------------------- writing

class StepOut(Out):
    step: int = Field(ge=1, le=3)
    subject: str = ""
    body: str


class SequenceOut(Out):
    steps: list[StepOut] = Field(min_length=3, max_length=3)
    facts_used: list[int] = Field(default_factory=list)
    proof_ids: list[str] = Field(default_factory=list)


class HumanizeOut(Out):
    steps: list[StepOut] = Field(min_length=3, max_length=3)


# ---------------------------------------------------------------- replies

class ReplyClass(Out):
    intent: Literal["interested", "not_now", "referral", "objection", "ooo", "bounce", "question", "other"]
    objection_type: Literal["reflex", "condition", "smokescreen", "genuine"] | None = None
    referral_name: str | None = None
    referral_title: str | None = None
    asks_if_automated: bool = False
    summary: str = ""


class ReplyDraftOut(Out):
    body: str
