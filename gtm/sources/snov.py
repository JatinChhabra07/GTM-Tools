"""Snov.io REST client (facts and sources: docs/tools.md §1). Used only by gtm code paths.

Paced at 1 request/second (Snov allows 60/min for the whole API). Async endpoints (verify, find)
return a task_hash first: callers save it as Step.external_ref BEFORE polling, so a killed run
resumes the same task instead of paying twice.
"""

from __future__ import annotations

import os
import threading
import time

import httpx

from gtm import secrets

BASE = "https://api.snov.io"
MIN_INTERVAL_S = 1.05
POLL_EVERY_S = 3
GENERIC_PREFIXES = ("info", "hello", "contact", "sales", "support", "team", "hi", "office", "admin",
                    "enquiries", "enquiry", "inquiries", "help", "mail", "marketing", "careers", "jobs",
                    "hr", "billing", "press", "media", "partners", "connect")


class SnovError(Exception):
    pass


class SnovUnavailable(SnovError):
    """Missing keys or API access (Snov API needs a paid plan)."""


class SnovCredits(SnovError):
    pass


def classify(smtp_status: str | None, unknown_reason: str | None) -> str:
    """Snov result -> our email_class. Catch-all is `unknown` + reason `catchall` (case varies)."""
    status = (smtp_status or "").lower()
    if status == "valid":
        return "valid"
    if status == "not_valid":
        return "invalid"
    if (unknown_reason or "").lower() == "catchall":
        return "catchall"
    return "unknown"


class Snov:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.client_id = client_id or secrets.get("SNOV_CLIENT_ID")
        self.client_secret = client_secret or secrets.get("SNOV_CLIENT_SECRET")
        if not (self.client_id and self.client_secret):
            raise SnovUnavailable("Snov keys missing: the founder runs `bin/gtm secrets set` in his terminal")
        self.http = httpx.Client(base_url=BASE, timeout=30, transport=transport, trust_env=False)
        self._token: str | None = None
        self._token_until = 0.0
        self._last = 0.0
        self._lock = threading.Lock()
        self.requests = 0

    # ---------------------------------------------------------- plumbing

    def _pace(self) -> None:
        with self._lock:
            wait = self._last + MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def token(self) -> str:
        if self._token and time.monotonic() < self._token_until - 60:
            return self._token
        self._pace()
        r = self.http.post("/v1/oauth/access_token", data={
            "grant_type": "client_credentials", "client_id": self.client_id,
            "client_secret": self.client_secret})
        if r.status_code in (401, 403):
            raise SnovUnavailable("Snov rejected the API keys (check them, and that your plan has API access)")
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._token_until = time.monotonic() + float(body.get("expires_in", 3600))
        return self._token

    def request(self, method: str, path: str, *, params=None, data=None, json=None, retries: int = 3):
        for attempt in range(retries + 1):
            self._pace()
            self.requests += 1
            headers = {"Authorization": f"Bearer {self.token()}"}
            # v1 samples pass access_token as a parameter; send both forms, harmless for v2
            if path.startswith("/v1/") and data is not None:
                data = {**data, "access_token": self.token()}
            elif path.startswith("/v1/"):
                params = {**(params or {}), "access_token": self.token()}
            try:
                r = self.http.request(method, path, params=params, data=data, json=json, headers=headers)
            except httpx.HTTPError as e:
                if attempt == retries:
                    raise SnovError(f"{method} {path}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 0) or 0) or 10 * (attempt + 1))
                continue
            if r.status_code == 401 and attempt < retries:
                self._token = None
                continue
            if r.status_code >= 500 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise SnovError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
            return r.json() if r.content else {}
        raise SnovError(f"{method} {path}: rate limited after {retries} retries")

    # ---------------------------------------------------------- verify / find (async)

    def verify_start(self, emails: list[str]) -> str:
        if not 1 <= len(emails) <= 10:
            raise ValueError("Snov verifies 1-10 emails per request")
        body = self.request("POST", "/v2/email-verification/start", params=[("emails[]", e) for e in emails])
        return body["data"]["task_hash"]

    def verify_result(self, task_hash: str) -> dict | None:
        """{email: {"email_class", "smtp_status", "unknown_status_reason", ...}} or None if running."""
        body = self.request("GET", "/v2/email-verification/result", params={"task_hash": task_hash})
        if body.get("status") != "completed":
            return None
        out = {}
        for row in body.get("data", []):
            res = row.get("result") or {}
            out[row["email"].lower()] = {
                "email_class": classify(res.get("smtp_status"), res.get("unknown_status_reason")),
                "smtp_status": res.get("smtp_status"),
                "unknown_status_reason": (res.get("unknown_status_reason") or None),
                "is_disposable": res.get("is_disposable"), "is_webmail": res.get("is_webmail"),
            }
        return out

    def wait(self, fn, task_hash: str, timeout_s: int = 180):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            res = fn(task_hash)
            if res is not None:
                return res
            time.sleep(POLL_EVERY_S)
        raise SnovError(f"task {task_hash} not finished after {timeout_s}s (resume later with the same hash)")

    def find_start(self, rows: list[dict]) -> str:
        """rows: [{first_name, last_name, domain}] (1-10)."""
        if not 1 <= len(rows) <= 10:
            raise ValueError("Snov finds 1-10 rows per request")
        body = self.request("POST", "/v2/emails-by-domain-by-name/start", json={"rows": rows})
        return body["data"]["task_hash"]

    def find_result(self, task_hash: str) -> dict | None:
        """{"First Last": [{"email", "email_class", ...}]} keyed by the full-name string Snov echoes."""
        body = self.request("GET", "/v2/emails-by-domain-by-name/result", params={"task_hash": task_hash})
        if body.get("status") == "not_enough_credits":
            raise SnovCredits("Snov: not enough credits")
        if body.get("status") != "completed":
            return None
        out: dict[str, list] = {}
        for row in body.get("data", []):
            out[row.get("people", "")] = [
                {"email": r["email"].lower(),
                 "email_class": classify(r.get("smtp_status"), r.get("unknown_status_reason")),
                 "smtp_status": r.get("smtp_status"),
                 "unknown_status_reason": r.get("unknown_status_reason")}
                for r in row.get("result") or [] if r.get("email")]
        return out

    def domain_emails_count(self, domain: str) -> int:
        """Free. 0 for webmail domains."""
        body = self.request("POST", "/v1/get-domain-emails-count", data={"domain": domain})
        return int(body.get("result") or 0)

    def domain_emails_start(self, domain: str) -> str:
        body = self.request("POST", "/v2/domain-search/domain-emails/start", data={"domain": domain})
        return (body.get("meta") or body.get("data") or {})["task_hash"]

    def domain_emails_result(self, task_hash: str) -> list[str] | None:
        body = self.request("GET", f"/v2/domain-search/domain-emails/result/{task_hash}")
        if str(body.get("status", "")).replace(" ", "_") == "in_progress":
            return None
        return [r["email"].lower() for r in body.get("data", []) if r.get("email")]

    # ---------------------------------------------------------- lists / sending

    def lists(self) -> list[dict]:
        rows = self.request("GET", "/v1/get-user-lists")
        return [r for r in rows if not r.get("isDeleted")]

    def create_list(self, name: str) -> int:
        body = self.request("POST", "/v1/lists", data={"name": name})
        body = body[0] if isinstance(body, list) else body
        return int(body["data"]["id"])

    def add_prospect(self, list_id: int, *, email: str, first_name: str, last_name: str,
                     company: str, position: str | None, custom_fields: dict[str, str]) -> dict:
        """Adding to a list with an active campaign STARTS the drip: call only after every gate."""
        data = {"listId": list_id, "email": email, "firstName": first_name, "lastName": last_name,
                "fullName": f"{first_name} {last_name}".strip(), "companyName": company,
                # true: a returning prospect's custom fields (our approved emails) are overwritten,
                # never left holding an older sequence's text
                "updateContact": "true", "createDuplicates": "false"}
        if position:
            data["position"] = position
        for k, v in custom_fields.items():
            data[f"customFields[{k}]"] = v
        body = self.request("POST", "/v1/add-prospect-to-list", data=data)
        if not body.get("success"):
            raise SnovError(f"add-prospect-to-list failed: {str(body.get('errors'))[:200]}")
        return body

    def campaigns(self) -> list[dict]:
        return self.request("GET", "/v1/get-user-campaigns")

    def campaign(self, campaign_id: int) -> dict:
        return self.request("GET", f"/v2/campaigns/{campaign_id}")

    def campaign_action(self, campaign_id: int, action: str) -> dict:
        if action not in ("start", "pause", "resume", "complete", "archived"):
            raise ValueError(action)
        return self.request("POST", f"/v2/campaigns/{campaign_id}/action", json={"action": action})

    def set_recipient_status(self, campaign_id: int, email: str, status: str) -> dict:
        if status not in ("Active", "Paused", "Unsubscribed"):
            raise ValueError(status)
        return self.request("POST", "/v1/change-recipient-status",
                            data={"email": email, "campaign_id": campaign_id, "status": status})

    def recipient_status(self, campaign_id: int, emails: list[str]) -> dict[str, str | None]:
        body = self.request("GET", f"/v2/campaigns/{campaign_id}/recipient",
                            params=[("emails[]", e) for e in emails[:100]])
        return {r["email"].lower(): r.get("status") for r in body.get("data", [])}

    # ---------------------------------------------------------- suppression

    def blacklists(self) -> list[dict]:
        return self.request("GET", "/v2/blacklists").get("data", [])

    def create_blacklist(self, name: str) -> int:
        return int(self.request("POST", "/v2/blacklists", json={"name": name})["data"]["list_id"])

    def blacklist_add(self, list_id: int, items: list[str]) -> dict:
        out = {"added": [], "duplicates": [], "rejected": []}
        for i in range(0, len(items), 100):
            body = self.request("POST", f"/v2/blacklists/{list_id}/items", json={"items": items[i:i + 100]})
            for k in out:
                out[k] += (body.get("data") or {}).get(k, [])
        return out

    # ---------------------------------------------------------- activity

    def replies(self, campaign_id: int) -> list[dict]:
        """Every reply with its text. Paged by offset."""
        out, offset, last_first = [], 0, None
        for _ in range(500):  # page size isn't documented: read until an empty or repeated page
            body = self.request("GET", f"/v2/campaigns/{campaign_id}/all-replies", params={"offset": offset})
            rows = body.get("data", []) if isinstance(body, dict) else body
            if not rows or rows[0] == last_first:
                return out
            last_first = rows[0]
            out += rows
            offset += len(rows)
        raise SnovError("all-replies paging did not end: refusing a partial read")

    def activity(self, campaign_id: int, date_from: str, date_to: str) -> list[dict]:
        """sent / open / click / reply / bounce / unsubscribe events (max 31-day window)."""
        out, offset = [], 0
        while True:
            body = self.request("GET", f"/v2/campaigns/{campaign_id}/recipients-activity",
                                params={"dateFrom": date_from, "dateTo": date_to, "offset": offset,
                                        "limit": 1000})
            out += body.get("data", [])
            pag = body.get("pagination") or {}
            if not pag.get("has_more"):
                return out
            offset += pag.get("limit", 1000)

    def finished(self, campaign_id: int) -> list[str]:
        rows = self.request("GET", "/v1/prospect-finished", params={"campaignId": campaign_id})
        return [r["userEmail"].lower() for r in rows or [] if r.get("userEmail")]

    def balance(self) -> dict:
        return self.request("GET", "/v1/get-balance").get("data", {})


def client() -> Snov:
    if os.environ.get("GTM_FAKE") == "1":
        from gtm import fake
        return fake.FakeSnov()
    return Snov()
