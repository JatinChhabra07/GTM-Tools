"""Apify actors via apify-client v3 (facts and sources: docs/tools.md §2).

Every run gets `max_items` AND `max_total_charge_usd`, so an actor can never overspend: Apify
stops it gracefully at the cap. The run id is returned before waiting, so callers save it as
Step.external_ref and a killed process resumes the same run instead of paying twice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from gtm import secrets

ACTORS = {
    "posts": "harvestapi/linkedin-post-search",
    "company": "harvestapi/linkedin-company",
    "employees": "harvestapi/linkedin-company-employees",
    "people": "harvestapi/linkedin-profile-search",
    "jobs": "curious_coder/linkedin-jobs-scraper",
    "tweets": "apidojo/tweet-scraper",
    "instagram": "apify/instagram-scraper",
    "google": "apify/google-search-scraper",
    "reddit": "trudax/reddit-scraper-lite",  # intent-sheet author lookup only (plan §15.5)
}

# Worst-case USD per result on the FREE/BRONZE tier (docs/tools.md §2.1) + per-run start fees.
# Used for --estimate and to size charge caps; Apify's own cap is the hard limit.
PRICE_PER_ITEM = {"posts": 0.002, "company": 0.004, "employees": 0.008, "people": 0.004,
                  "jobs": 0.002, "tweets": 0.0004, "instagram": 0.0027, "google": 0.0045,
                  "reddit": 0.004}
PRICE_PER_RUN = {"employees": 0.02, "people": 0.10}  # actor-start / first search page
MIN_CHARGE = {"posts": 0.01, "company": 0.004, "employees": 0.05, "people": 0.1, "jobs": 0.001,
              "tweets": 0.0, "instagram": 0.0027, "google": 0.5,
              "reddit": 0.01}
TERMINAL = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}


class ApifyError(Exception):
    pass


class ApifyUnavailable(ApifyError):
    pass


def estimate(kind: str, items: int, runs: int = 1) -> float:
    return round(PRICE_PER_ITEM[kind] * items + PRICE_PER_RUN.get(kind, 0.0) * runs, 4)


@dataclass
class RunInfo:
    run_id: str
    status: str
    dataset_id: str | None
    usage_usd: float


class Apify:
    def __init__(self, token: str | None = None):
        token = token or secrets.get("APIFY_API_TOKEN")
        if not token:
            raise ApifyUnavailable("Apify key missing: the founder runs `bin/gtm secrets set` in his terminal")
        from apify_client import ApifyClient
        self.client = ApifyClient(token, max_retries=4)

    def start(self, kind: str, run_input: dict, *, max_items: int, max_charge_usd: float) -> str:
        cap = max(max_charge_usd, MIN_CHARGE[kind])
        run = self.client.actor(ACTORS[kind]).start(
            run_input=run_input, max_items=max_items,
            max_total_charge_usd=Decimal(str(round(cap, 4))))
        return run.id

    def wait(self, run_id: str, minutes: int = 15) -> RunInfo:
        run = self.client.run(run_id).wait_for_finish(wait_duration=timedelta(minutes=minutes))
        if run is None:
            raise ApifyError(f"run {run_id} not found")
        return RunInfo(run.id, str(run.status), run.default_dataset_id, float(run.usage_total_usd or 0))

    def items(self, info: RunInfo, limit: int = 1000) -> list[dict]:
        if not info.dataset_id:
            return []
        return list(self.client.dataset(info.dataset_id).list_items(limit=limit).items)

    def month_spend_usd(self) -> float | None:
        try:
            limits = self.client.user().limits()
            return float(limits.current.monthly_usage_usd)
        except Exception:  # informational only (plan §1.3: quota readings never drive control)
            return None


def client() -> Apify:
    if os.environ.get("GTM_FAKE") == "1":
        from gtm import fake
        return fake.FakeApify()
    return Apify()


# ---------------------------------------------------------------- input builders (docs/tools.md §2.2)

def posts_input(queries: list[str], *, max_posts: int, posted_limit: str = "week") -> dict:
    return {"searchQueries": queries, "maxPosts": max_posts, "postedLimit": posted_limit,
            "sortBy": "date", "profileScraperMode": "short", "scrapeReactions": False,
            "scrapeComments": False}


def company_input(*, urls: list[str] = (), names: list[str] = ()) -> dict:
    out: dict = {}
    if urls:
        out["companies"] = list(urls)
    if names:
        out["searches"] = list(names)
    return out


def employees_input(company_urls: list[str], *, job_titles: list[str], per_company: int) -> dict:
    return {"companies": company_urls, "jobTitles": job_titles, "profileScraperMode": "Full ($8 per 1k)",
            "maxItemsPerCompany": per_company, "companyBatchMode": "one_by_one"}


def people_input(*, titles: list[str], company_urls: list[str] = (), locations: list[str] = (),
                 query: str = "", pages: int = 1) -> dict:
    out = {"profileScraperMode": "Full", "currentJobTitles": titles, "takePages": pages}
    if company_urls:
        out["currentCompanies"] = list(company_urls)
    if locations:
        out["locations"] = list(locations)
    if query:
        out["searchQuery"] = query
    return out


def jobs_input(keywords: str, *, location: str = "", date_posted: str = "pastWeek", limit: int = 50) -> dict:
    out = {"keywords": keywords, "datePosted": date_posted, "scrapeCompany": True, "limitPerSource": limit}
    if location:
        out["location"] = location
    return out


def tweets_input(*, handles: list[str] = (), terms: list[str] = (), since: str | None = None) -> dict:
    out: dict = {"sort": "Latest", "tweetLanguage": "en"}
    if handles:
        out["twitterHandles"] = list(handles)
    if terms:
        out["searchTerms"] = list(terms)
    if since:
        out["start"] = since
    return out


def google_input(queries: list[str], *, country: str = "us", pages: int = 1) -> dict:
    return {"queries": "\n".join(queries), "maxPagesPerQuery": pages, "countryCode": country,
            "saveHtmlToKeyValueStore": False, "saveHtml": False, "includeIcons": False}
