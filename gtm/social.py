"""Social intent upgrade (plan §15.1 E1): per-source switches, free fallback sources, the intent
sheet, and the per-product source test.

Anonymous authors (most Reddit/HN/forum posts) never become leads. Their intent posts go to the
intent sheet, a CSV the founder follows up by hand; nothing is ever sent to them automatically.
A post that names the poster's company still makes that company an account (gtm/resolve.py).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta

from gtm.config import SOCIAL_SOURCES, ProductConfig
from gtm.paths import ensure_private_dir, product_data_dir
from gtm.util import domain_of, has_pii, now, now_iso, parse_iso, sanitize_name, sha256

PAID = ("linkedin", "x", "instagram")
# key -> (label, domains wigolo search is scoped to, query suffix). Free: wigolo search.
FALLBACKS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "hn_web": ("Hacker News (web search)", ("news.ycombinator.com",), ""),
    "quora": ("Quora", ("quora.com",), ""),
    "indiehackers": ("Indie Hackers", ("indiehackers.com",), ""),
    "forums": ("Niche forums", (), " forum"),
    "reviews": ("Review sites", ("g2.com", "capterra.com", "trustradius.com", "trustpilot.com"), " review"),
    "jobs": ("Job boards", ("indeed.com", "wellfound.com", "weworkremotely.com", "glassdoor.com"), " job"),
}
FORUM_EXCLUDE = ("reddit.com", "quora.com", "linkedin.com", "x.com", "twitter.com", "facebook.com",
                 "instagram.com", "youtube.com", "medium.com", "news.ycombinator.com", "indiehackers.com")
LABELS = {"linkedin": "LinkedIn posts", "x": "X posts", "instagram": "Instagram posts", "reddit": "Reddit",
          "hackernews": "Hacker News", **{k: v[0] for k, v in FALLBACKS.items()}}
STALE_DAYS = 90  # a dated post older than this is not current intent


def source_on(cfg: ProductConfig, name: str) -> bool:
    """The founder's switch, else the default: core on, Instagram per instagram.enabled, fallbacks off."""
    s = cfg.signals.sources
    if name in s:
        return bool(s[name])
    if name == "instagram":
        return bool((cfg.signals.instagram or {}).get("enabled"))
    return name not in FALLBACKS


def keywords(cfg: ProductConfig, n: int = 3) -> list[str]:
    li = [k for k in (cfg.signals.linkedin or {}).get("keywords", []) if k]
    pl = [q for q in (cfg.signals.pain_language or {}).get("queries", []) if q]
    return (li or pl or [f"{cfg.product.name} alternatives"])[:n]


_REDDIT_USER = re.compile(r"^[A-Za-z0-9_-]{3,20}$")
_HN_USER = re.compile(r"^[A-Za-z0-9_-]{2,15}$")
_IG_USER = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def profile_url(source: str, author: str | None) -> str | None:
    """Public profile of a community author, only from a username the source itself gave us."""
    a = (author or "").strip()
    if source == "reddit" and _REDDIT_USER.match(a) and a.lower() not in ("deleted", "removed", "automoderator"):
        return f"https://www.reddit.com/user/{a}"
    if source == "hackernews" and _HN_USER.match(a):
        return f"https://news.ycombinator.com/user?id={a}"
    if source == "instagram" and _IG_USER.match(a) and not a.startswith("."):
        return f"https://www.instagram.com/{a}/"
    return None


def _fresh(posted: str | None) -> bool:
    if not posted:
        return True
    try:
        return now() - parse_iso(str(posted)) <= timedelta(days=STALE_DAYS)
    except ValueError:
        return True


# ---------------------------------------------------------------- intent sheet

COLUMNS = ("date", "source", "post_url", "author_profile_url", "intent_code", "summary", "company_if_named")
RETENTION_DAYS = 90


def sheet_path(slug: str):
    return product_data_dir(slug) / "intent_sheet.csv"


def _blocked_path(slug: str):
    return product_data_dir(slug) / "intent_sheet_removed.txt"


def _cell(value) -> str:
    """One CSV cell: single line, no control chars, and never a spreadsheet formula."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def _url(value: str | None) -> str:
    v = (value or "").strip()
    return v if re.match(r"^https?://[^\s\"'<>]+$", v) and len(v) <= 500 else ""


def clean_summary(text: str | None) -> str:
    return "" if has_pii(text or "") else sanitize_name(text or "", 160)


def sheet_row(*, source: str, post_url: str, author_profile_url: str | None, intent_code: str,
              summary: str | None, company: str | None, date: str | None = None) -> dict:
    return {"date": date or now().date().isoformat(), "source": source, "post_url": _url(post_url),
            "author_profile_url": _url(author_profile_url), "intent_code": intent_code,
            "summary": clean_summary(summary), "company_if_named": sanitize_name(company or "", 80)}


def read_sheet(slug: str) -> list[dict]:
    path = sheet_path(slug)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [{c: r.get(c) or "" for c in COLUMNS} for r in csv.DictReader(f)]


def _write_sheet(slug: str, rows: list[dict]) -> None:
    path = sheet_path(slug)
    ensure_private_dir(path.parent)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: _cell(r.get(c)) for c in COLUMNS})
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _blocked(slug: str) -> set[str]:
    p = _blocked_path(slug)
    return set(p.read_text().split()) if p.exists() else set()


def add_rows(slug: str, rows: list[dict]) -> int:
    """Append new rows; dedupe by post_url; skip anything the founder removed. Returns rows added."""
    current = read_sheet(slug)
    seen = {r["post_url"] for r in current}
    blocked = _blocked(slug)
    new = []
    for r in rows:
        url = r.get("post_url") or ""
        if not url or url in seen or sha256(url) in blocked or \
                (r.get("author_profile_url") and sha256(r["author_profile_url"]) in blocked):
            continue
        seen.add(url)
        new.append(r)
    if new:
        _write_sheet(slug, current + new)
    return len(new)


def remove(slug: str, url: str) -> int:
    """Erasure on request: drop rows whose post or author profile is `url`, and never re-add them."""
    url = url.strip()
    rows = read_sheet(slug)
    keep = [r for r in rows if url not in (r["post_url"], r["author_profile_url"])]
    ensure_private_dir(product_data_dir(slug))
    p = _blocked_path(slug)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(sha256(url) + "\n")
    if len(keep) != len(rows):
        _write_sheet(slug, keep)
    return len(rows) - len(keep)


def prune_sheet(slug: str, days: int = RETENTION_DAYS) -> int:
    """Retention (docs/compliance.md): rows older than `days` are dropped; the file stays."""
    rows = read_sheet(slug)
    cutoff = (now() - timedelta(days=days)).date().isoformat()
    keep = [r for r in rows if (r["date"] or "") >= cutoff]
    if len(keep) != len(rows):
        _write_sheet(slug, keep)
    return len(rows) - len(keep)


def recent_rows(slug: str, since_days: int) -> list[dict]:
    cutoff = (now() - timedelta(days=since_days)).date().isoformat()
    return [r for r in read_sheet(slug) if (r["date"] or "") >= cutoff]


# ---------------------------------------------------------------- fallback sources (wigolo, free)

def _queries(key: str, kws: list[str]) -> list[str]:
    return [f"{k}{FALLBACKS[key][2]}" for k in kws]


def _search_args(key: str, kws: list[str], max_results: int) -> dict:
    domains = FALLBACKS[key][1]
    # titles + snippets are all we read: no page fetches. (search_depth "fast" returned nothing for
    # site-scoped queries in the live check, so the default depth stays.)
    args = {"query": _queries(key, kws), "max_results": max_results, "time_range": "month",
            "include_content": False}
    if domains:
        args["include_domains"] = list(domains)
    else:
        args["exclude_domains"] = list(FORUM_EXCLUDE)
    return args


def _search_many(calls: list[tuple[str, dict]]) -> list:
    """All fallback searches on ONE wigolo server process. A failed call comes back as an exception."""
    from gtm.sources import wigolo
    if os.environ.get("GTM_FAKE") == "1":
        out = []
        for name, args in calls:
            try:
                out.append(wigolo.call(name, args))
            except Exception as e:
                out.append(e)
        return out
    return asyncio.run(wigolo.call_many(calls))


_STOPWORDS = {"with", "that", "this", "from", "your", "what", "best", "tool", "tools", "software", "alternative",
              "alternatives", "looking", "need", "help", "using", "forum", "review", "reviews"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 4 and w not in _STOPWORDS}


def _relevant(text: str, kws: list[str]) -> bool:
    """Web search returns a lot of junk (home pages, ads, unrelated sites). Keep a result only if its
    title/snippet shares at least two significant words with one keyword (one, for a one-word keyword)."""
    words = _tokens(text)
    return any(len(words & t) >= min(2, len(t)) for t in map(_tokens, kws) if t)


def fallback_items(key: str, res, kws: list[str]) -> list[dict]:
    """wigolo results -> anonymous post items: http(s) pages (not home pages) on the scoped domains
    whose title/snippet/URL words match a keyword. Q&A sites often block snippets but put the question
    in the URL ("/What-is-a-good-cart-recovery-tool"), so the URL words go to the labeller too."""
    from urllib.parse import unquote, urlparse
    domains = FALLBACKS[key][1]
    out = []
    for r in (res.get("results", []) if isinstance(res, dict) else []):
        url = _url(r.get("url"))
        d = domain_of(url)
        if not url or (domains and not any(d == x or d.endswith("." + x) for x in domains)):
            continue
        if re.match(r"^https?://[^/]+/?(\?.*)?$", url):  # a home page is not a post
            continue
        if not domains and any(d == x or d.endswith("." + x) for x in FORUM_EXCLUDE):
            continue
        posted = (r.get("freshness_signal") or {}).get("published_date") if isinstance(r.get("freshness_signal"), dict) else None
        if not _fresh(posted):
            continue
        slug = re.sub(r"[-_/+.]+", " ", unquote(urlparse(url).path)).strip()[:200]
        text = f"{r.get('title') or ''}\n{r.get('snippet') or ''}\nURL WORDS: {slug}".strip()
        if _relevant(text, kws):
            out.append({"source": key, "url": url, "text": text, "posted_at": posted, "author_name": None,
                        "author_title": None, "author_profile": None, "company_page": False})
    return out


def collect_fallbacks(ctx) -> None:
    """Run step (full mode): the fallback sources the founder enabled. Stored as anonymous posts."""
    from gtm.signals import store_post
    on = [k for k in FALLBACKS if source_on(ctx.cfg, k)]
    if not on:
        return
    kws = keywords(ctx.cfg)
    try:
        results = _search_many([("search", _search_args(k, kws, 10)) for k in on])
    except Exception as e:  # wigolo/npx down: the paid and last30days sources still ran
        ctx.note(f"fallback sources skipped: wigolo failed ({type(e).__name__})")
        return
    for key, res in zip(on, results, strict=True):
        if isinstance(res, Exception):
            ctx.note(f"{LABELS[key]} search failed ({type(res).__name__})")
            continue
        new = sum(store_post(ctx.conn, source=key, url=it["url"], text=it["text"], posted_at=it["posted_at"],
                             author_name=None, author_handle=None, author_title=None, company_page=False,
                             keywords=kws, run_id=ctx.run_id) for it in fallback_items(key, res, kws))
        ctx.count(f"posts_{key}", new)


def community_items(export_items: list[dict]) -> list[dict]:
    """last30days items -> anonymous post items (Reddit/HN), the poster's own words only."""
    out = []
    for it in export_items:
        src = it.get("source")
        if src not in ("reddit", "hackernews") or not _fresh(it.get("posted_at")):
            continue
        url = _url(it.get("discussion_url") if src == "hackernews" else None) or _url(it.get("url"))
        text = it.get("own_text") or f"{it.get('title') or ''}\n{it.get('text') or ''}"
        if url and text.strip():
            out.append({"source": src, "url": url, "text": text, "posted_at": it.get("posted_at"),
                        "author_name": None, "author_title": None,
                        "author_profile": profile_url(src, it.get("author")), "company_page": False})
    return out


# ---------------------------------------------------------------- source test (gtm signals test)

PROBE_ITEMS = {"linkedin": 10, "x": 20, "instagram": 10}
SAMPLE_PER_SOURCE = 5
TEST_CALL_CAP = 6


@dataclass
class _ProbeCtx:  # what gtm.sources.optional() needs
    notes: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def probe_estimate() -> dict[str, float]:
    from gtm.sources import apify
    return {"linkedin": max(apify.estimate("posts", PROBE_ITEMS["linkedin"]), apify.MIN_CHARGE["posts"]),
            "x": max(apify.estimate("tweets", PROBE_ITEMS["x"]), apify.MIN_CHARGE["tweets"]),
            "instagram": max(apify.estimate("instagram", PROBE_ITEMS["instagram"]), apify.MIN_CHARGE["instagram"])}


def _apify_probe(kind: str, run_input: dict, n: int, budget, what: str) -> list[dict]:
    from gtm.sources import apify
    client = apify.client()  # ApifyUnavailable (no key) -> optional() skips with a note
    run_id = client.start(kind, run_input, max_items=n,
                          max_charge_usd=budget.reserve_apify(apify.estimate(kind, n), what))
    info = client.wait(run_id, minutes=10)
    budget.charge_apify(info.usage_usd, what, run_id)
    return client.items(info, limit=n)


def _paid_items(source: str, kws: list[str], budget) -> list[dict]:
    from gtm.sources import apify
    n = PROBE_ITEMS[source]
    if source == "linkedin":
        out = []
        for it in _apify_probe("posts", apify.posts_input(kws, max_posts=n, posted_limit="month"), n, budget,
                               "source test: LinkedIn posts"):
            a = it.get("author") or {}
            if (it.get("linkedinUrl") or "").startswith("http") and it.get("content"):
                out.append({"source": "linkedin", "url": it["linkedinUrl"], "text": it["content"],
                            "posted_at": (it.get("postedAt") or {}).get("date"), "author_name": a.get("name"),
                            "author_title": a.get("info"), "author_profile": a.get("linkedinUrl"),
                            "company_page": bool(a.get("universalName")) and not a.get("publicIdentifier")})
        return out
    if source == "x":
        out = []
        for t in _apify_probe("tweets", apify.tweets_input(terms=kws), n, budget, "source test: X posts"):
            a = t.get("author") or {}
            if t.get("url") and t.get("text") and not t.get("isRetweet"):
                out.append({"source": "x", "url": t["url"], "text": t["text"], "posted_at": t.get("createdAt"),
                            "author_name": a.get("name"), "author_title": a.get("description"),
                            "author_profile": a.get("url"), "company_page": False})
        return out
    tags = [re.sub(r"[^A-Za-z0-9_]", "", k) for k in kws][:3]
    run_input = {"search": ",".join(t for t in tags if t), "searchType": "hashtag", "resultsLimit": n, "searchLimit": 3}
    return [{"source": "instagram", "url": p["url"], "text": p["caption"], "posted_at": p.get("timestamp"),
             "author_name": None, "author_title": None, "author_profile": profile_url("instagram", p.get("ownerUsername")),
             "company_page": False}
            for p in _apify_probe("instagram", run_input, n, budget, "source test: Instagram")
            if p.get("url") and p.get("caption")]


def label_items(session, cfg: ProductConfig, conn, items: list[dict], allowed: list[str]) -> dict[str, dict]:
    """Label in-memory items (isolated, batched). Returns url -> {code, employer, named}."""
    from gtm.schemas import PostLabels
    from gtm.signals import LABEL_BATCH, _category_text, clean_excerpt, post_block
    meanings = {r["code"]: r["meaning"] for r in conn.execute(
        f"SELECT code, meaning FROM signal_codes WHERE code IN ({','.join('?' * len(allowed))})", allowed)}
    out: dict[str, dict] = {}
    for i in range(0, len(items), LABEL_BATCH):
        batch = items[i:i + LABEL_BATCH]
        ids = {f"t{i + j}": it for j, it in enumerate(batch)}
        payload = {"product": cfg.product.name, "category": _category_text(cfg), "competitors": cfg.signals.competitors,
                   "intent_codes": allowed, "meanings": meanings, "post_ids": list(ids)}
        blocks = {f"post:{pid}": post_block(source=it["source"], author_name=it["author_name"],
                                            author_title=it["author_title"], company_page=it["company_page"],
                                            text=clean_excerpt(it["text"])) for pid, it in ids.items()}
        res = session.json("label_posts", payload, PostLabels, untrusted_blocks=blocks)
        for lab in (res.labels if res else []):
            it = ids.get(lab.post_id)
            if not it:
                continue
            code = lab.code if lab.code in allowed and not lab.drop_reason else None
            employer = (lab.employer or "").strip()
            out[it["url"]] = {"code": code, "employer": bool(employer),
                              "named": bool(it["author_name"]) and bool(employer)}
    return out


def _recommend(r: dict) -> tuple[str, str]:
    if r["status"] != "ok":
        return "unknown", {"needs_confirm": "paid probe not run (re-run with --confirm)",
                           "skipped": "no key: probe skipped", "error": "probe failed"}.get(r["status"], r["status"])
    if r["items"] == 0:
        return "off", "found nothing for these keywords"
    if r["intent"] == 0:
        return "off", f"none of {r['sampled']} sampled posts showed buying intent"
    return "on", f"{r['intent']} of {r['sampled']} sampled posts showed buying intent"


def source_test(slug: str, cfg: ProductConfig, conn, *, confirm: bool) -> dict:
    """Probe every source once for this product's keywords; label a small sample; recommend on/off.
    Paid probes (Apify) run only with confirm=True; a missing key skips them. Writes source_test.json."""
    from gtm import llm
    from gtm import observations as obs
    from gtm.budget import Budget
    from gtm.signals import INTENT_CODES
    from gtm.sources import last30days, optional
    kws = keywords(cfg)
    est = probe_estimate()
    pctx = _ProbeCtx()
    results: dict[str, dict] = {s: {"source": s, "label": LABELS[s], "paid": s in PAID, "status": "ok",
                                    "currently_on": source_on(cfg, s), "items": 0} for s in SOCIAL_SOURCES}
    found: dict[str, list[dict]] = {s: [] for s in SOCIAL_SOURCES}
    budget = Budget(round(sum(est.values()) * 1.5 + 0.05, 2), 0)
    for s in PAID:
        results[s]["estimate_usd"] = est[s]
        if not confirm:
            results[s]["status"] = "needs_confirm"
            continue
        try:
            items = optional(pctx, f"{LABELS[s]} probe", _paid_items, s, kws, budget)
        except Exception as e:
            results[s].update(status="error", error=type(e).__name__)
            continue
        if items is None:
            results[s]["status"] = "skipped"
        else:
            found[s] = items
    try:
        export = last30days.run(kws[0], timeout=300)
        for it in community_items(last30days.items(export)):
            found[it["source"]].append(it)
    except Exception as e:  # Last30Error carries our own message; anything else: its type only
        msg = str(e)[:160] if isinstance(e, last30days.Last30Error) else type(e).__name__
        for s in ("reddit", "hackernews"):
            results[s].update(status="error", error=msg)
    fb = list(FALLBACKS)
    try:
        searched = _search_many([("search", _search_args(k, kws, 10)) for k in fb])
    except Exception as e:
        searched = [e] * len(fb)
    for key, res in zip(fb, searched, strict=True):
        if isinstance(res, Exception):
            results[key].update(status="error", error=type(res).__name__)
        else:
            found[key] = fallback_items(key, res, kws)
    allowed = sorted(obs.confirmed_codes(conn, "intent") & set(INTENT_CODES)) or \
        ["ASKS_FOR_SOLUTION", "COMPETITOR_PAIN", "SWITCHING"]
    sample = [it for s in SOCIAL_SOURCES for it in found[s][:SAMPLE_PER_SOURCE]]
    session = llm.Session(conn, run_id=f"srctest-{now_iso()[:10]}", cap=TEST_CALL_CAP, model=cfg.usage.model,
                          pause_minutes=cfg.usage.usage_limit_pause_minutes)
    labels: dict[str, dict] = {}
    if sample:
        try:
            labels = label_items(session, cfg, conn, sample, allowed)
        except (llm.CallCapReached, llm.UsageLimit, llm.LLMError) as e:
            pctx.note(f"labelling stopped early: {type(e).__name__}")
    for s in SOCIAL_SOURCES:
        r = results[s]
        if r["status"] != "ok":
            r.update(recommend=_recommend(r)[0], reason=_recommend(r)[1])
            continue
        sampled = [it for it in found[s][:SAMPLE_PER_SOURCE] if it["url"] in labels]
        intent = [it for it in sampled if labels[it["url"]]["code"]]
        resolvable = [it for it in intent if labels[it["url"]]["named"] or labels[it["url"]]["employer"]]
        r.update(items=len(found[s]), sampled=len(sampled), intent=len(intent), resolvable=len(resolvable),
                 share_resolvable=round(len(resolvable) / len(intent), 2) if intent else 0.0,
                 examples=[it["url"] for it in intent[:3]])
        if found[s] and not sampled:
            r.update(recommend="unknown", reason="not labelled (isolated-call budget or usage limit)")
        else:
            r["recommend"], r["reason"] = _recommend(r)
    out = {"product": slug, "tested_at": now_iso(), "keywords": kws, "intent_codes": allowed,
           "isolated_calls": session.calls, "apify_usd": round(budget.spent_apify_usd, 4),
           "paid_estimate_usd": round(sum(est.values()), 4), "notes": pctx.notes,
           "results": [results[s] for s in SOCIAL_SOURCES]}
    path = ensure_private_dir(product_data_dir(slug)) / "source_test.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(out, indent=2))
    out["path"] = str(path)
    return out


def format_table(res: dict) -> str:
    head = f"{'source':<26} {'items':>5} {'intent':>10} {'resolvable':>10}  recommend  reason"
    lines = [head, "-" * len(head)]
    for r in res["results"]:
        intent = f"{r.get('intent', 0)}/{r.get('sampled', 0)}" if r["status"] == "ok" else "-"
        share = f"{int(100 * r.get('share_resolvable', 0))}%" if r["status"] == "ok" else "-"
        on = " (now on)" if r["currently_on"] else ""
        lines.append(f"{r['label'] + on:<26} {r.get('items', 0):>5} {intent:>10} {share:>10}  "
                     f"{r['recommend']:<9}  {sanitize_name(r['reason'], 70)}")
    return "\n".join(lines)
