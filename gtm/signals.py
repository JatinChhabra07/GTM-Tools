"""Signal-first engine (plan §5 Phase 4): named people expressing need.

LinkedIn posts (Apify), X named accounts (Apify), Instagram (opt-in), and Reddit/HN through
last30days (pain language for the writer, and intent posts since D3), plus the free fallback sources
in gtm/social.py. A post becomes a lead candidate only as `named_person`: a real name AND employer
evidence written by the author. Anonymous intent posts go to the intent sheet for manual follow-up;
one that names the poster's company still makes that company an account (gtm/resolve.py).
"""

from __future__ import annotations

import json
import re

from gtm import kb, llm, social
from gtm import observations as obs
from gtm.schemas import PainPhrases, PostLabels
from gtm.sources import optional
from gtm.util import has_pii, now, now_iso, sha1

INTENT_CODES = ("ASKS_FOR_SOLUTION", "COMPETITOR_PAIN", "SWITCHING", "HIRING_FOR_PROBLEM")
LABEL_BATCH = 15
COMMUNITY_LABEL_CALLS = 2  # Reddit/HN/Instagram/fallback posts: at most 2 isolated calls per run
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
    on = lambda s: social.source_on(ctx.cfg, s)  # noqa: E731  (the founder's per-source switches)
    if codes & {"ASKS_FOR_SOLUTION", "COMPETITOR_PAIN", "SWITCHING"}:
        if on("linkedin"):
            optional(ctx, "LinkedIn posts", _linkedin, ctx)
        if ctx.mode == "full":
            if on("x"):
                optional(ctx, "X posts", _x, ctx)
            if on("instagram"):
                optional(ctx, "Instagram posts", _instagram, ctx)
    if ctx.mode == "full":
        if codes & set(INTENT_CODES):
            social.collect_fallbacks(ctx)
        pain_language(ctx)  # also stores Reddit/HN posts for intent labelling
    label(ctx)
    if on("reddit") and ctx.cfg.signals.reddit_authors.enabled:
        optional(ctx, "Reddit author lookup", social.lookup_reddit_authors, ctx)


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
    terms = [t for t in x.get("keywords", []) if t][:5]  # set by /gtm-icp when the source test says X works
    if not handles and not terms:
        return
    items = _apify_run(ctx, "tweets", "x_tweets", apify.tweets_input(handles=handles, terms=terms), 50,
                       "X posts")
    new = 0
    for t in items:
        a = t.get("author") or {}
        if not t.get("url") or not t.get("text") or t.get("isRetweet"):
            continue
        new += store_post(ctx.conn, source="x", url=t["url"], text=t["text"], posted_at=t.get("createdAt"),
                          author_name=a.get("name"), author_handle=a.get("url"), author_title=None,
                          company_page=False, keywords=handles + terms, run_id=ctx.run_id)
    ctx.count("posts_x", new)


def _instagram(ctx) -> None:
    ig = ctx.cfg.signals.instagram or {}  # on/off: social.source_on (instagram.enabled by default)
    from gtm.sources import apify
    tags = [t.lstrip("#") for t in ig.get("hashtags", []) if t][:3]
    if not tags:
        return
    items = _apify_run(ctx, "instagram", "instagram", {"search": ",".join(tags), "searchType": "hashtag",
                                                       "resultsLimit": 20, "searchLimit": 3}, 40, "Instagram hashtags")
    for p in items:
        if p.get("url") and p.get("caption"):
            # Instagram handles can't be resolved to a work identity: never a lead (no author_name);
            # the profile link only goes to the intent sheet
            store_post(ctx.conn, source="instagram", url=p["url"], text=p["caption"], posted_at=p.get("timestamp"),
                       author_name=None, author_handle=social.profile_url("instagram", p.get("ownerUsername")),
                       author_title=None, company_page=False, keywords=tags, run_id=ctx.run_id)


# ---------------------------------------------------------------- labelling (isolated)

def label(ctx) -> None:
    allowed = sorted(obs.confirmed_codes(ctx.conn, "intent") & set(INTENT_CODES))
    if not allowed:
        return
    meanings = {r["code"]: r["meaning"] for r in ctx.conn.execute(
        f"SELECT code, meaning FROM signal_codes WHERE code IN ({','.join('?' * len(allowed))})", allowed)}
    rows = ctx.conn.execute("SELECT * FROM posts WHERE labelled_at IS NULL AND source IN ('linkedin','x') "
                            "ORDER BY posted_at DESC LIMIT 60").fetchall()
    _label_rows(ctx, rows, allowed, meanings)
    rows = ctx.conn.execute("SELECT * FROM posts WHERE labelled_at IS NULL AND source NOT IN ('linkedin','x') "
                            "ORDER BY posted_at DESC LIMIT ?", (COMMUNITY_LABEL_CALLS * LABEL_BATCH,)).fetchall()
    _label_rows(ctx, rows, allowed, meanings, max_calls=COMMUNITY_LABEL_CALLS)


def post_block(*, source: str, author_name: str | None, author_title: str | None, company_page: bool,
               text: str) -> str:
    return (f"SOURCE: {source}\nAUTHOR: {author_name or '-'}\nAUTHOR INFO: {author_title or '-'}\n"
            f"PAGE TYPE: {'company page' if company_page else 'person'}\nTEXT: {text}")


_STOP = object()


def _label_call(ctx, payload: dict, blocks: dict, cap_at: int | None):
    """One label_posts call. With cap_at the session cap is lowered for this call only, so invalid-output
    retries can't take the pass past its share of the run's isolated calls."""
    if cap_at is None:
        return ctx.llm.json("label_posts", payload, PostLabels, untrusted_blocks=blocks)
    real = ctx.llm.cap
    ctx.llm.cap = min(real, cap_at)
    try:
        return ctx.llm.json("label_posts", payload, PostLabels, untrusted_blocks=blocks)
    except llm.CallCapReached:
        if ctx.llm.calls >= real:  # the run's own cap: the run ends partial as before
            raise
        return _STOP
    finally:
        ctx.llm.cap = real


def _label_rows(ctx, rows, allowed: list[str], meanings: dict, max_calls: int | None = None) -> None:
    start, sheet = ctx.llm.calls, []
    try:
        for i in range(0, len(rows), LABEL_BATCH):
            batch = rows[i:i + LABEL_BATCH]
            if ctx.llm.remaining < 12:  # keep calls for qualify + write, which matter more
                msg = "isolated-call budget low: some posts left unlabelled for the next run"
                if msg not in ctx.notes:  # both passes may hit it
                    ctx.note(msg)
                return
            if max_calls is not None and ctx.llm.calls - start >= max_calls:
                ctx.note("community posts: this run's label calls are used; the rest wait for the next run")
                return
            payload = {"product": ctx.cfg.product.name, "category": _category(ctx),
                       "competitors": ctx.cfg.signals.competitors, "intent_codes": allowed,
                       "meanings": meanings, "post_ids": [r["id"] for r in batch]}
            blocks = {f"post:{r['id']}": post_block(source=r["source"], author_name=r["author_name"],
                                                    author_title=r["author_title"],
                                                    company_page=r["resolvability"] == "company_event",
                                                    text=r["text_excerpt"]) for r in batch}
            out = _label_call(ctx, payload, blocks, None if max_calls is None else start + max_calls)
            if out is _STOP:
                ctx.note("community posts: this run's label calls are used; the rest wait for the next run")
                return
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
                    if resolv != "named_person":  # can't become a lead: the founder's manual follow-up sheet
                        handle = r["author_handle"] if (r["author_handle"] or "").startswith("http") else None
                        sheet.append(social.sheet_row(source=r["source"], post_url=r["url"], author_profile_url=handle,
                                                      intent_code=code, summary=lab.summary, company=employer))
    finally:
        if sheet:
            ctx.count("intent_sheet_rows", social.add_rows(ctx.slug, sheet))


def _category(ctx) -> str:
    return _category_text(ctx.cfg)


def _category_text(cfg) -> str:
    hits = kb.search(cfg.product.slug, "what the product does", category="product_fact", k=3)
    return " ".join(h["text"] for h in hits)[:600] or cfg.product.name


# ---------------------------------------------------------------- pain language (last30days)

def pain_language(ctx) -> None:
    """Weekly last30days pass over the enabled community sources: pain vocabulary for the writer and,
    when an intent code is confirmed, Reddit/HN posts stored for intent labelling (anonymous)."""
    srcs = tuple(s for s in ("reddit", "hackernews") if social.source_on(ctx.cfg, s))
    if not srcs:
        return
    pl = ctx.cfg.signals.pain_language or {}
    row = ctx.conn.execute("SELECT last_collected_at FROM watermarks WHERE source = 'last30days'").fetchone()
    from gtm.util import days_ago
    if row and days_ago(row[0]) < 6:  # weekly is plenty: this is vocabulary, not leads
        return
    topics = [t for t in pl.get("queries", []) if t][:2] or [ctx.cfg.product.name + " alternatives"]
    from gtm.sources import last30days
    texts = []
    ok = False
    store = bool(obs.confirmed_codes(ctx.conn, "intent") & set(INTENT_CODES))
    for t in topics:
        try:
            export = last30days.run(t, sources=srcs, subreddits=pl.get("subreddits") or None)
            found = [i for i in last30days.items(export) if i["source"] in srcs]
            texts += [f"{i['title']}\n{i['text']}" for i in found[:15]]
            ok = True
            for it in (social.community_items(found) if store else []):
                ctx.count(f"posts_{it['source']}", store_post(
                    ctx.conn, source=it["source"], url=it["url"], text=it["text"], posted_at=it["posted_at"],
                    author_name=None, author_handle=it["author_profile"], author_title=None, company_page=False,
                    keywords=[t], run_id=ctx.run_id))
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
