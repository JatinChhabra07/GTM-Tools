"""Signal-first engine (plan §5 Phase 4): named people expressing need.

LinkedIn posts (Apify), X named accounts (Apify), Instagram (opt-in), and Reddit/HN through
last30days, which only ever becomes `pain_language` in the KB, never a lead. A post becomes a
lead candidate only as `named_person`: a real name AND employer evidence written by the author.
"""

from __future__ import annotations

import json
import re

from gtm import kb
from gtm import observations as obs
from gtm.schemas import PainPhrases, PostLabels
from gtm.sources import optional
from gtm.util import has_pii, now, now_iso, sha1

INTENT_CODES = ("ASKS_FOR_SOLUTION", "COMPETITOR_PAIN", "SWITCHING", "HIRING_FOR_PROBLEM")
LABEL_BATCH = 15
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏ -‮⁠﻿]")


def clean_excerpt(text: str, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", _CTRL.sub("", text or "")).strip()[:limit]


def store_post(conn, *, source: str, url: str, text: str, posted_at: str | None, author_name: str | None,
               author_handle: str | None, author_title: str | None, company_page: bool, keywords: list[str],
               run_id: str) -> bool:
    pid = sha1(source, url)
    cur = conn.execute(
        "INSERT OR IGNORE INTO posts (id, source, url, posted_at, author_name, author_handle, author_title, "
        "text_excerpt, resolvability, matched_keywords_json, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pid, source, url, posted_at, (author_name or "")[:120] or None, (author_handle or "")[:200] or None,
         (author_title or "")[:200] or None, clean_excerpt(text),
         "company_event" if company_page else "anonymous", json.dumps(keywords), run_id))
    return cur.rowcount == 1


def collect(ctx) -> None:
    codes = obs.confirmed_codes(ctx.conn, "intent")
    if codes & {"ASKS_FOR_SOLUTION", "COMPETITOR_PAIN", "SWITCHING"}:
        optional(ctx, "LinkedIn posts", _linkedin, ctx)
        if ctx.mode == "full":
            optional(ctx, "X posts", _x, ctx)
            optional(ctx, "Instagram posts", _instagram, ctx)
    label(ctx)
    if ctx.mode == "full":
        pain_language(ctx)


# ---------------------------------------------------------------- sources

def _apify_run(ctx, kind: str, ref: str, run_input: dict, max_items: int, what: str) -> list[dict]:
    from gtm.sources import apify
    client = apify.client()
    run_id = ctx.get_ref("collect").get(ref)
    if not run_id:
        cap = ctx.budget.reserve_apify(apify.estimate(kind, max_items), what)
        run_id = client.start(kind, run_input, max_items=max_items, max_charge_usd=cap)
        ctx.set_ref("collect", ref, run_id)
    info = client.wait(run_id)
    ctx.budget.charge_apify(info.usage_usd, what, run_id)
    return client.items(info, limit=max_items)


def _posted_limit(ctx, source: str) -> str:
    row = ctx.conn.execute("SELECT last_collected_at FROM watermarks WHERE source = ?", (source,)).fetchone()
    if row:
        from gtm.util import days_ago
        return "24h" if days_ago(row[0]) < 1.5 else "week"
    return "week"


def _watermark(ctx, source: str) -> None:
    ctx.conn.execute("INSERT INTO watermarks (source, last_collected_at) VALUES (?, ?) ON CONFLICT(source) "
                     "DO UPDATE SET last_collected_at = excluded.last_collected_at", (source, now_iso()))


def _linkedin(ctx) -> None:
    from gtm.sources import apify
    li = ctx.cfg.signals.linkedin or {}
    keywords = [k for k in li.get("keywords", []) if k][:8]
    keywords += [f'"{c}"' for c in ctx.cfg.signals.competitors[:4]]
    if not keywords:
        return
    max_posts = min(ctx.cfg.budgets.max_items_per_actor, 60 if ctx.mode == "full" else 25)
    items = _apify_run(ctx, "posts", "linkedin_posts",
                       apify.posts_input(keywords, max_posts=max_posts, posted_limit=_posted_limit(ctx, "linkedin_posts")),
                       max_posts, "LinkedIn post search")
    new = 0
    for it in items:
        author = it.get("author") or {}
        url = it.get("linkedinUrl") or ""
        if not url.startswith("http") or not it.get("content"):
            continue
        company_page = bool(author.get("universalName")) and not author.get("publicIdentifier")
        posted = ((it.get("postedAt") or {}).get("date"))
        new += store_post(ctx.conn, source="linkedin", url=url, text=it["content"], posted_at=posted,
                          author_name=author.get("name"), author_handle=author.get("linkedinUrl"),
                          author_title=author.get("info"), company_page=company_page,
                          keywords=keywords, run_id=ctx.run_id)
    ctx.count("posts_linkedin", new)
    _watermark(ctx, "linkedin_posts")


def _x(ctx) -> None:
    from gtm.sources import apify
    x = ctx.cfg.signals.x or {}
    handles = [h.lstrip("@") for h in x.get("named_accounts", []) if h][:10]
    if not handles:
        return
    items = _apify_run(ctx, "tweets", "x_tweets", apify.tweets_input(handles=handles), 50, "X named accounts")
    new = 0
    for t in items:
        a = t.get("author") or {}
        if not t.get("url") or not t.get("text") or t.get("isRetweet"):
            continue
        new += store_post(ctx.conn, source="x", url=t["url"], text=t["text"], posted_at=t.get("createdAt"),
                          author_name=a.get("name"), author_handle=a.get("url"), author_title=None,
                          company_page=False, keywords=handles, run_id=ctx.run_id)
    ctx.count("posts_x", new)


def _instagram(ctx) -> None:
    ig = ctx.cfg.signals.instagram or {}
    if not ig.get("enabled"):
        return
    from gtm.sources import apify
    tags = [t.lstrip("#") for t in ig.get("hashtags", []) if t][:3]
    if not tags:
        return
    items = _apify_run(ctx, "instagram", "instagram", {"search": ",".join(tags), "searchType": "hashtag",
                                                       "resultsLimit": 20, "searchLimit": 3}, 40, "Instagram hashtags")
    for p in items:
        if p.get("url") and p.get("caption"):
            # Instagram handles can't be resolved to a work identity: theme input only, never a lead
            store_post(ctx.conn, source="instagram", url=p["url"], text=p["caption"], posted_at=p.get("timestamp"),
                       author_name=None, author_handle=None, author_title=None, company_page=False,
                       keywords=tags, run_id=ctx.run_id)


# ---------------------------------------------------------------- labelling (isolated)

def label(ctx) -> None:
    allowed = sorted(obs.confirmed_codes(ctx.conn, "intent") & set(INTENT_CODES))
    if not allowed:
        return
    rows = ctx.conn.execute("SELECT * FROM posts WHERE labelled_at IS NULL AND source IN ('linkedin','x') "
                            "ORDER BY posted_at DESC LIMIT 60").fetchall()
    meanings = {r["code"]: r["meaning"] for r in ctx.conn.execute(
        f"SELECT code, meaning FROM signal_codes WHERE code IN ({','.join('?' * len(allowed))})", allowed)}
    for i in range(0, len(rows), LABEL_BATCH):
        batch = rows[i:i + LABEL_BATCH]
        if ctx.llm.remaining < 12:  # keep calls for qualify + write, which matter more
            ctx.note("isolated-call budget low: some posts left unlabelled for the next run")
            return
        payload = {"product": ctx.cfg.product.name, "category": _category(ctx),
                   "competitors": ctx.cfg.signals.competitors, "intent_codes": allowed,
                   "meanings": meanings, "post_ids": [r["id"] for r in batch]}
        blocks = {f"post:{r['id']}": f"AUTHOR: {r['author_name'] or '-'}\nAUTHOR INFO: {r['author_title'] or '-'}\n"
                                     f"PAGE TYPE: {'company page' if r['resolvability'] == 'company_event' else 'person'}\n"
                                     f"TEXT: {r['text_excerpt']}" for r in batch}
        out = ctx.llm.json("label_posts", payload, PostLabels, untrusted_blocks=blocks)
        by_id = {lab.post_id: lab for lab in (out.labels if out else [])}
        for r in batch:
            lab = by_id.get(r["id"])
            code = lab.code if lab and lab.code in allowed and not lab.drop_reason else None
            employer = (lab.employer or "").strip()[:120] if lab else ""
            resolv = r["resolvability"]
            if resolv != "company_event":
                resolv = "named_person" if (r["author_name"] and employer) else "anonymous"
            ctx.conn.execute("UPDATE posts SET intent_code = ?, intent_strength = ?, employer = ?, resolvability = ?, "
                             "labelled_at = ? WHERE id = ?",
                             (code, lab.strength if (lab and code) else None, employer or None, resolv, now_iso(), r["id"]))
            if code:
                ctx.count("posts_with_intent")


def _category(ctx) -> str:
    hits = kb.search(ctx.slug, "what the product does", category="product_fact", k=3)
    return " ".join(h["text"] for h in hits)[:600] or ctx.cfg.product.name


# ---------------------------------------------------------------- pain language (last30days)

def pain_language(ctx) -> None:
    pl = ctx.cfg.signals.pain_language or {}
    row = ctx.conn.execute("SELECT last_collected_at FROM watermarks WHERE source = 'last30days'").fetchone()
    from gtm.util import days_ago
    if row and days_ago(row[0]) < 6:  # weekly is plenty: this is vocabulary, not leads
        return
    topics = [t for t in pl.get("queries", []) if t][:2] or [ctx.cfg.product.name + " alternatives"]
    from gtm.sources import last30days
    texts = []
    ok = False
    for t in topics:
        try:
            export = last30days.run(t, subreddits=pl.get("subreddits") or None)
            texts += [f"{i['title']}\n{i['text']}" for i in last30days.items(export)[:15]]
            ok = True
        except last30days.Last30Error as e:  # our own message (never scraped text): safe to show
            ctx.note(f"last30days failed for one topic: {str(e)[:160]}")
        except Exception as e:
            ctx.note(f"last30days failed for one topic: {type(e).__name__}")
    if ok:  # only a successful pass waits a week; a failure is retried next full run
        _watermark(ctx, "last30days")
    if not texts or ctx.llm.remaining < 12:
        return
    out = ctx.llm.json("extract_pain", {"product": ctx.cfg.product.name, "category": _category(ctx)},
                       PainPhrases, untrusted_blocks={f"community:{i}": t for i, t in enumerate(texts[:20])})
    added = 0
    for p in (out.phrases if out else []):
        p = p.strip()
        if 3 <= len(p.split()) <= 12 and not has_pii(p):
            try:
                added += kb.add(ctx.slug, "pain_language", p, extra={"learned": now().date().isoformat()})["status"] == "added"
            except kb.KBError:
                pass
    ctx.count("pain_phrases", added)
