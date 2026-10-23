"""GTM_FAKE=1: a small, consistent fake world so every pipeline step runs offline (plan §0 #10).

Fake websites, Apify datasets, Snov answers, wigolo results and isolated-call answers. The
answers are deterministic and only as smart as the tests need; the real behaviour lives in the
modules under test. Tests can tweak the world through the module-level dicts.
"""

from __future__ import annotations

import json
import re

from gtm.public_http import Page, PublicHTTPError

# ---------------------------------------------------------------- the world

COMPANIES = {
    "shopco.example": {"name": "ShopCo", "industry": "E-commerce", "size": (51, 200), "country": "United States",
                       "linkedin": "https://www.linkedin.com/company/shopco",
                       "people": [("Priya Sharma", "Head of Growth"), ("Tom Reed", "Office Manager")],
                       "jobs": ["CRO Manager", "Growth Marketer"], "tech": "cdn.shopify.com static.klaviyo.com"},
    "brandly.example": {"name": "Brandly", "industry": "E-commerce", "size": (11, 50), "country": "United States",
                        "linkedin": "https://www.linkedin.com/company/brandly",
                        "people": [("Sam Carter", "Founder & CEO")], "jobs": [], "tech": "cdn.shopify.com"},
    "rivalcart.example": {"name": "RivalCart", "industry": "Software", "size": (51, 200), "country": "United States",
                          "linkedin": None, "people": [("Ana Lopez", "VP Marketing")], "jobs": [], "tech": ""},
    "clientco.example": {"name": "ClientCo", "industry": "E-commerce", "size": (51, 200), "country": "United States",
                         "linkedin": None, "people": [], "jobs": [], "tech": "cdn.shopify.com static.klaviyo.com"},
}
POSTS: list[dict] = [
    {"linkedinUrl": "https://www.linkedin.com/posts/sam-carter-1", "content":
        "We keep losing carts at checkout. Can anyone recommend a cart recovery tool that works with Shopify?",
     "author": {"name": "Sam Carter", "info": "Founder & CEO at Brandly", "publicIdentifier": "sam-carter",
                "linkedinUrl": "https://www.linkedin.com/in/sam-carter"}, "postedAt": {"date": None}},
    {"linkedinUrl": "https://www.linkedin.com/posts/anon-2", "content": "Cart abandonment is brutal this season.",
     "author": {"name": "", "info": ""}, "postedAt": {"date": None}},
]
SENT: list[dict] = []          # prospects pushed to the fake Snov
REPLIES: dict[int, list] = {}  # campaign_id -> all-replies records
ACTIVITY: dict[int, list] = {}  # campaign_id -> recipients-activity events
SNOV_FAIL = {"add_prospect": False, "replies": False}
SNOV_VALID_PATTERN = "first.last"


def reset() -> None:
    SENT.clear()
    REPLIES.clear()
    ACTIVITY.clear()
    SNOV_FAIL.update({"add_prospect": False, "replies": False})


# ---------------------------------------------------------------- websites (profiler)

def _html(domain: str, path: str) -> str | None:
    c = COMPANIES.get(domain)
    if not c:
        return None
    if path in ("/", ""):
        ld = json.dumps({"@context": "https://schema.org", "@type": "Organization", "name": c["name"],
                         "address": {"addressCountry": c["country"]},
                         "sameAs": [c["linkedin"]] if c["linkedin"] else []})
        return (f"<html><head><title>{c['name']}</title><script type='application/ld+json'>{ld}</script>"
                f"<script src='https://{c['tech'].split(' ')[0] if c['tech'] else 'example.org'}/x.js'></script>"
                + "".join(f"<script src='https://{t}/a.js'></script>" for t in c["tech"].split()[1:]) +
                f"</head><body><a href='/about'>About</a><a href='/careers'>Careers</a><a href='/contact'>Contact</a>"
                f"<a href='tel:+1 (415) 555-0100'>Call us</a>"
                f"<p>{c['name']} sells things online.</p></body></html>")
    if path == "/about":
        people = "".join(f"<div><h3>{n}</h3><p>{t}</p></div>" for n, t in c["people"])
        return f"<html><body><h1>Our team</h1>{people}</body></html>"
    if path == "/careers":
        return "<html><body>" + "".join(f"<h3>{j}</h3>" for j in c["jobs"]) + "</body></html>"
    if path == "/contact":
        return f"<html><body><a href='mailto:hello@{domain}'>hello@{domain}</a></body></html>"
    return None


class FakeHTTP:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def close(self):
        return None

    def get(self, url: str) -> Page:
        m = re.match(r"https?://(?:www\.)?([^/]+)(/[^?#]*)?", url)
        if not m:
            raise PublicHTTPError(f"bad url {url}")
        domain, path = m.group(1), (m.group(2) or "/").rstrip("/") or "/"
        html = _html(domain, path)
        if html is None:
            if domain in COMPANIES:
                return Page(url=url, status=404, content_type="text/html", text="", headers={})
            raise PublicHTTPError(f"unreachable: {domain}")
        return Page(url=url, status=200, content_type="text/html; charset=utf-8", text=html, headers={})


# ---------------------------------------------------------------- wigolo / last30days

def wigolo(name: str, args: dict):
    if name == "cache":
        return {"stats": {"total_urls": 0}}
    if name == "search":
        q = " ".join(args["query"]) if isinstance(args.get("query"), list) else str(args.get("query"))
        results = []
        for d, c in COMPANIES.items():
            if c["name"].lower() in q.lower() or "companies" in q or "brands" in q or "like" in q:
                results.append({"url": f"https://{d}/", "title": f"{c['name']} official site", "snippet": c["industry"]})
        results.append({"url": "https://www.g2.com/categories/x", "title": "Top 10 tools", "snippet": "list"})
        return {"results": results}
    if name == "fetch":
        if "/lists/" in args["url"]:  # a "top brands" article naming fake companies
            md = "# Top D2C brands\n" + "\n".join(f"{i}. [{c['name']}](https://{d}/?ref=list)"
                                                  for i, (d, c) in enumerate(COMPANIES.items(), 1))
            return {"url": args["url"], "markdown": md + "\nFollow us [here](https://twitter.com/lists)",
                    "links": ["https://twitter.com/lists", "https://www.listsite.example/about"]}
        return {"url": args["url"], "markdown": "fake page"}
    if name == "crawl":
        root = args["url"].rstrip("/")
        body = "Acme recovers abandoned Shopify carts with plain follow-up emails. " * 8
        return {"pages": [{"url": f"{root}{p}", "title": t, "markdown": f"# {t}\n\n{body}"}
                          for p, t in (("/", "Acme"), ("/pricing", "Pricing"), ("/customers", "Customers"),
                                       ("/blog", "Blog"))] + [{"url": "https://elsewhere.example/", "title": "x",
                                                                "markdown": body}]}
    if name == "diff":
        old, new = args["old"].get("markdown", ""), args["new"].get("markdown", "")
        return {"changed": old != new, "summary": {"total_changed_chars": abs(len(new) - len(old)) + (old != new) * 500}}
    return {}


def last30days(topic: str) -> dict:
    return {"items_by_source": {"reddit": [
        {"url": "https://reddit.com/r/shopify/1", "title": "carts dying at shipping step",
         "body": "Our carts die at the shipping step and nobody comes back", "published_at": "2026-10-01"}]}}


# ---------------------------------------------------------------- Apify

class _Info:
    def __init__(self, run_id, items):
        self.run_id, self.status, self.dataset_id, self.usage_usd = run_id, "SUCCEEDED", run_id, 0.01
        self.items = items


class FakeApify:
    started: list[tuple[str, dict]] = []

    def start(self, kind: str, run_input: dict, *, max_items: int, max_charge_usd: float) -> str:
        FakeApify.started.append((kind, run_input))
        return f"run-{kind}-{len(FakeApify.started)}"

    def wait(self, run_id: str, minutes: int = 15):
        kind = run_id.split("-")[1]
        return _Info(run_id, _apify_items(kind))

    def items(self, info, limit: int = 1000) -> list[dict]:
        return info.items[:limit]

    def month_spend_usd(self):
        return 0.0


def _apify_items(kind: str) -> list[dict]:
    if kind == "posts":
        return list(POSTS)
    if kind == "company":
        out = []
        for d, c in COMPANIES.items():
            out.append({"linkedinUrl": c["linkedin"], "website": f"https://{d}", "name": c["name"],
                        "industries": [{"name": c["industry"]}], "employeeCountRange": {"start": c["size"][0], "end": c["size"][1]},
                        "locations": [{"country": c["country"], "headquarter": True}]})
        return out
    if kind == "employees":
        out = []
        for d, c in COMPANIES.items():
            for n, t in c["people"]:
                first, last = n.split(" ", 1)
                out.append({"firstName": first, "lastName": last, "headline": t,
                            "linkedinUrl": f"https://www.linkedin.com/in/{first.lower()}-{last.lower()}",
                            "location": {"countryCode": "US"},
                            "experience": [{"position": t, "companyName": c["name"], "companyLinkedinUrl": c["linkedin"]}]})
        return out
    if kind == "jobs":
        return [{"title": j, "link": f"https://www.linkedin.com/jobs/view/{i}", "companyName": c["name"],
                 "companyWebsite": f"https://{d}", "companyLinkedinUrl": c["linkedin"]}
                for i, (d, c) in enumerate(COMPANIES.items()) for j in c["jobs"]]
    return []


# ---------------------------------------------------------------- Snov

class FakeSnov:
    def __init__(self):
        self._tasks: dict[str, list] = {}

    def balance(self):
        return {"balance": "100.00"}

    def verify_start(self, emails):
        h = f"v{len(self._tasks)}"
        self._tasks[h] = list(emails)
        return h

    def _status(self, email: str) -> dict:
        local, domain = email.split("@", 1)
        ok = (SNOV_VALID_PATTERN == "first.last" and "." in local) or local == "hello"
        cls = "valid" if ok else "invalid"
        return {"email_class": cls, "smtp_status": "valid" if ok else "not_valid", "unknown_status_reason": None}

    def verify_result(self, h):
        return {e.lower(): self._status(e) for e in self._tasks[h]}

    def find_start(self, rows):
        h = f"f{len(self._tasks)}"
        self._tasks[h] = rows
        return h

    def find_result(self, h):
        return {f"{r['first_name']} {r['last_name']}": [{"email": f"{r['first_name']}.{r['last_name']}@{r['domain']}".lower(),
                                                         "email_class": "valid", "smtp_status": "valid",
                                                         "unknown_status_reason": None}]
                for r in self._tasks[h]}

    def wait(self, fn, h, timeout_s=180):
        return fn(h)

    def add_prospect(self, list_id, **kw):
        if SNOV_FAIL["add_prospect"]:
            raise TimeoutError("fake network timeout after the request was sent")
        SENT.append({"list_id": list_id, **kw})
        return {"success": True, "id": f"p{len(SENT)}", "added": True}

    def replies(self, campaign_id):
        if SNOV_FAIL["replies"]:
            raise ConnectionError("fake Snov outage")
        return REPLIES.get(campaign_id, [])

    def activity(self, campaign_id, date_from, date_to):
        return ACTIVITY.get(campaign_id, [])

    def finished(self, campaign_id):
        return []

    def campaign_action(self, campaign_id, action):
        return {"success": True}

    def set_recipient_status(self, campaign_id, email, status):
        return {"success": True}

    def blacklist_add(self, list_id, items):
        return {"added": items, "duplicates": [], "rejected": []}

    def campaign(self, campaign_id):
        return {"data": {"tracking": {"open": False, "link_click": False},
                         "sending_settings": {"skip_company_domain_who_replied": True}}}


# ---------------------------------------------------------------- isolated-call answers

def _ids(blocks: dict, prefix: str) -> list[tuple[str, str]]:
    return [(k.split(":", 1)[1], v) for k, v in blocks.items() if k.startswith(prefix)]


def llm_response(prompt_id: str, payload: dict, blocks: dict) -> dict:
    if prompt_id == "noop":
        return {"ok": True}
    if prompt_id == "label_posts":
        labels = []
        for pid, text in _ids(blocks, "post:"):
            low = text.lower()
            code = "ASKS_FOR_SOLUTION" if ("recommend" in low or "looking for" in low) else None
            info = re.search(r"AUTHOR INFO: .* at (.+)", text)
            labels.append({"post_id": pid, "code": code, "strength": 3 if code else None,
                           "employer": info.group(1).strip() if info else None})
        return {"labels": labels}
    if prompt_id == "extract_pain":
        return {"phrases": ["carts die at the shipping step"]}
    if prompt_id == "classify_candidates":
        urls = re.findall(r"URL: (\S+)", blocks.get("search_results", ""))
        return {"companies": [{"url": u, "is_company_site": "g2.com" not in u and "/lists/" not in u,
                               "is_company_list": "/lists/" in u, "name": u} for u in urls]}
    if prompt_id == "resolve_employers":
        out = []
        for emp, text in _ids(blocks, "results:"):
            url = next((u for u in re.findall(r"URL: (\S+)", text) if emp.lower().replace(" ", "") in u), None)
            out.append({"employer": emp, "domain": re.sub(r"https?://([^/]+).*", r"\1", url) if url else None,
                        "evidence_url": url, "confidence": 0.9 if url else 0})
        return {"resolutions": out}
    if prompt_id == "label_news":
        return {"events": [{"domain": c["domain"], "code": None} for c in payload.get("companies", [])]}
    if prompt_id == "persona_match":
        return {"matches": [{"title": t, "persona": None} for t in payload.get("titles", [])]}
    if prompt_id == "qualify_worker":
        return {"leads": [{"lead_key": k, "assessment": "fits", "penalty": 0} for k in payload["lead_keys"]]}
    if prompt_id == "qualify_review":
        out = []
        for lk, sheet in _ids(blocks, "lead:"):
            bad = "rivalcart" in sheet.lower() and payload.get("lens") in ("risk", "truth_risk")
            out.append({"lead_key": lk, "verdict": "drop" if bad else "ok", "reason": "competitor" if bad else ""})
        return {"findings": out}
    if prompt_id in ("qualify_synth", "qualify_recheck"):
        findings = payload.get("findings") or payload.get("open_questions") or {}
        return {"verdicts": [{"lead_key": k, "verdict": "drop" if any(f["verdict"] == "drop" for f in findings.get(k, []))
                              else "keep", "penalty": 0, "reason": ""} for k in payload["lead_keys"]]}
    if prompt_id == "shortlist_worker":
        return {"accounts": [{"domain": d, "assessment": "fits", "penalty": 0} for d in payload["domains"]]}
    if prompt_id == "shortlist_review":
        out = []
        for d, sheet in _ids(blocks, "account:"):
            bad = "panel-drop" in sheet.lower() and payload.get("lens") in ("risk", "truth_risk")
            out.append({"domain": d, "verdict": "drop" if bad else "ok", "reason": "fake drop" if bad else ""})
        return {"findings": out}
    if prompt_id == "shortlist_synth":
        f = payload["findings"]
        return {"verdicts": [{"domain": d, "verdict": "drop" if any(x["verdict"] == "drop" for x in f.get(d, []))
                              else "keep", "penalty": 0, "reason": ""} for d in payload["domains"]]}
    if prompt_id == "write_sequence":
        r = payload["recipient"]
        g = r["greeting"]
        return {"steps": [
            {"step": 1, "subject": "checkout drop-off", "body":
                f"{g}, teams selling on Shopify often lose buyers between cart and payment. "
                f"The fix is usually in the follow-up, not the ads.\n\nIs cart recovery something you're working on?"},
            {"step": 2, "subject": "", "body": f"{g}, one more thought. Most stores send one reminder and stop. "
                                               "A second, plainer note tends to bring back more carts. Worth a look?"},
            {"step": 3, "subject": "", "body": f"{g}, I'll leave it here. If carts ever become a priority, just reply."}],
            "facts_used": [payload["primary_signal"]["id"]], "proof_ids": []}
    if prompt_id == "humanize":
        return {"steps": payload["steps"]}
    if prompt_id == "classify_reply":
        text = blocks.get("reply", "").lower()
        if "talk to" in text:
            name = re.search(r"talk to ([a-z]+ [a-z]+)", text)
            return {"intent": "referral", "referral_name": name.group(1).title() if name else None, "summary": "referral"}
        return {"intent": "interested" if "interested" in text or "tell me more" in text else "other", "summary": "x"}
    if prompt_id == "draft_reply":
        return {"body": "Thanks for getting back to me. Happy to share how it works. What does your setup look like today?"}
    raise KeyError(f"fake has no answer for prompt {prompt_id}")
