"""Deterministic email validator (plan §5 Phase 7). Pure functions: no network, no database.

It runs at write time, at edit time and again at push time. Prompts ask the writer for these rules;
this module enforces them, so a bad generation can't reach a prospect (Harvey's gate.py idea, much
stricter: Harvey allowed 220 words and only warned about a missing postal address).

`validate_sequence` returns every violation, not just the first, so `gtm review` can show the
founder everything wrong with a draft at once. An empty list means the sequence may be approved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

WORD_CAPS = {1: 75, 2: 75, 3: 40}
MAX_LINKS_PER_SEQUENCE = 1
MAX_QUOTE_WORDS = 15
SUBJECT_WORDS = (2, 5)
REFERENCE_POLICIES = ("self_public_professional", "company_event", "theme_only")

# Replies the opt-out handler always honours (Harvey config.py OPT_OUT_WORDS, extended). The
# footer must ask for one of these, so a prospect who follows our instructions is always heard.
OPT_OUT_KEYWORDS: tuple[str, ...] = (
    "no thanks", "no thank you", "not interested", "unsubscribe", "stop", "remove me",
    "take me off", "opt out", "opt-out", "do not email", "don't email", "do not contact",
    "don't contact",
)

PLAYBOOKS_DIR = Path(__file__).resolve().parent.parent / "playbooks"

# Steps 2-3 go out days after they were written, so "this week" becomes a lie.
TIME_WORDS = (
    "recent", "recently", "just saw", "just noticed", "just read", "just now", "today",
    "yesterday", "this morning", "this week", "last week", "this month", "last month",
    "the other day", "earlier this week", "a few days ago",
)
PLATFORM_WORDS = ("reddit", "subreddit", "instagram", "insta")
# Phrases that say we watched the lead's own activity; allowed only if the lead wrote the signal.
YOUR_POST_PHRASES = (
    "your post", "your tweet", "your comment", "your thread", "your linkedin post",
    "you posted", "you tweeted", "you wrote", "you commented",
)
# theme_only = colleague-authored or anonymous: the pain theme only, never where we saw it.
SOURCE_WORDS = ("linkedin", "twitter", "tweet", "on x", "a post", "a thread", "a forum")
OPENER_WORDS = {"i", "we", "i'm", "i've", "i'd", "i'll", "we're", "we've", "we'd", "we'll"}

_WORD = re.compile(r"[^\W_]+(?:['-][^\W_]+)*")
_MERGE_TAGS = (
    re.compile(r"\{\{[^{}]*\}\}"),                      # {{first_name}}
    re.compile(r"\{\s*[A-Za-z_][\w .-]*\}"),            # {first_name}
    re.compile(r"\[\s*[A-Za-z_][A-Za-z0-9_ ]*\]"),       # [FIRST_NAME], [company]
    re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%"),             # %FIRSTNAME%
    re.compile(r"\{\{|\}\}"),                           # a half-rendered tag
)
_HTML = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>|&(?:nbsp|amp|lt|gt|quot|#\d+);")
_TLDS = r"(?:com|io|co|ai|net|org|in|app|dev|so|xyz|us|uk|me|biz|info|tech|ly|gg)"
_URL = re.compile(
    r"(?:https?://|www\.)[^\s<>\"')\]]+"
    rf"|(?<![@\w.-])(?:[a-z0-9-]+\.)+{_TLDS}\b(?:/[^\s<>\"')\]]*)?",
    re.IGNORECASE,
)
# A number claim: 40, 3.5, 1,200, $2M, ₹50k, 30%, 3x, 10+.
_NUMBER = re.compile(r"[$₹€£]?\d[\d,]*(?:\.\d+)?\s?(?:%|x\b|k\b|m\b|bn\b|cr\b|\+)?", re.IGNORECASE)
_FAKE_REPLY = re.compile(r"^\s*(?:re|fwd?)\s*:", re.IGNORECASE)
_SALUTATION = re.compile(r"^\s*(?:(?:hi|hey|hello|dear)\b[^,\n]{0,40}[,\n]|(?:team|all|folks)\s*,)",
                         re.IGNORECASE)


@dataclass
class Step:
    step: int        # 1..3
    subject: str     # step 1 subject; steps 2-3 may be "" (same thread)
    body: str        # body WITHOUT footer


@dataclass
class Context:
    recipient_email: str
    db_recipient_email: str
    lead_first_name: str
    lead_full_name: str
    company: str
    is_signal_author: bool
    reference_policy: str
    allowed_urls: list[str]
    proof_quotes: list[str]
    claimable_names: list[str]
    signal_texts: list[str]
    notice: str | None       # None = this geo needs no notice; "" = needed but not configured
    sender_name: str
    postal_address: str
    opt_out_line: str
    banned_extra: list[str]


@dataclass
class Violation:
    rule: str
    step: int | None
    message: str


# ---------------------------------------------------------------- rendering


def render_footer(ctx: Context) -> str:
    lines = [ctx.sender_name, ctx.postal_address, ctx.notice or "", ctx.opt_out_line]
    return "\n".join(line.strip() for line in lines if line and line.strip())


def render_step(step: Step, ctx: Context) -> str:
    """Exactly what gets sent (and hashed for approval)."""
    return step.body.strip() + "\n\n" + render_footer(ctx)


# ---------------------------------------------------------------- text helpers


def _norm(text: str) -> str:
    """Lowercase, straight quotes, single spaces: so a curly apostrophe can't dodge a match."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def words(text: str) -> list[str]:
    return _WORD.findall(text.replace("’", "'"))


def _phrase_re(phrase: str) -> re.Pattern:
    p = _norm(phrase)
    # Word boundaries only where the phrase edge is a word char, so "—" still matches mid-word.
    left = r"(?<![\w'])" if p[:1].isalnum() else ""
    right = r"(?![\w'])" if p[-1:].isalnum() else ""
    return re.compile(left + re.escape(p) + right)


def find_phrases(text: str, phrases) -> list[str]:
    """Which of `phrases` occur in `text` (case/quote-insensitive, whole words)."""
    t = _norm(text)
    return [p for p in phrases if p.strip() and _phrase_re(p).search(t)]


def is_opt_out_phrase(text: str) -> bool:
    return bool(find_phrases(text, OPT_OUT_KEYWORDS))


def find_urls(text: str) -> list[str]:
    return [m.rstrip(".,;:!?") for m in _URL.findall(text)]


def _url_key(url: str) -> str:
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[,\s]", "", m).rstrip("+").lower() for m in _NUMBER.findall(text)}


def _without(text: str, *names: str) -> str:
    for name in names:
        if name and name.strip():
            text = re.sub(re.escape(name.strip()), " ", text, flags=re.IGNORECASE)
    return text


def _ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    w = [x.lower() for x in words(text)]
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def _opening_word(body: str, first_name: str) -> str:
    text = _SALUTATION.sub("", body.lstrip(), count=1).lstrip()
    if first_name:  # "Sam, I noticed..." starts with "I" once the name is stripped
        text = re.sub(rf"^{re.escape(first_name)}\s*[,:\-]\s*", "", text, flags=re.IGNORECASE)
    first = words(text[:40])
    return first[0].lower() if first else ""


# ---------------------------------------------------------------- banned phrases


def load_banned_phrases(playbooks_dir: Path | None = None) -> list[str]:
    """The `- phrase` lines under `## Banned phrases` in playbooks/writing.md."""
    path = (playbooks_dir or PLAYBOOKS_DIR) / "writing.md"
    phrases: list[str] = []
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip().lower() == "banned phrases"
            continue
        if in_section and line.startswith("- "):
            phrase = line[2:].strip().strip("`").strip()
            if phrase:
                phrases.append(phrase.lower())
    if not phrases:
        # An empty list would silently pass every email; refuse instead.
        raise ValueError(f"no '## Banned phrases' list found in {path}")
    return phrases


@lru_cache(maxsize=4)
def _default_banned(playbooks_dir: Path) -> tuple[str, ...]:
    return tuple(load_banned_phrases(playbooks_dir))


# ---------------------------------------------------------------- rules


def validate_sequence(steps: list[Step], ctx: Context, *,
                      banned_phrases: list[str] | None = None) -> list[Violation]:
    """Every rule, every step. `banned_phrases` defaults to playbooks/writing.md."""
    banned = list(banned_phrases if banned_phrases is not None else _default_banned(PLAYBOOKS_DIR))
    banned += [b.lower() for b in ctx.banned_extra if b.strip()]
    out: list[Violation] = []
    out += _check_shape(steps)
    for s in steps:
        out += _check_step(s, ctx, banned)
    out += _check_sequence_links(steps, ctx)
    out += _check_recipient(ctx)
    out += _check_footer(ctx)
    if ctx.reference_policy not in REFERENCE_POLICIES:
        out.append(Violation("reference_policy", None,
                             f"Unknown reference policy {ctx.reference_policy!r}."))
    return out


def _check_shape(steps: list[Step]) -> list[Violation]:
    numbers = [s.step for s in steps]
    if numbers != list(range(1, len(WORD_CAPS) + 1)):
        # the Snov campaign always sends 3 steps from gtm_body_1..3: a missing step would go out empty
        return [Violation("sequence_shape", None,
                          f"A sequence has exactly {len(WORD_CAPS)} emails numbered 1..{len(WORD_CAPS)}; got {numbers}.")]
    return []


def _check_step(s: Step, ctx: Context, banned: list[str]) -> list[Violation]:
    out: list[Violation] = []

    def v(rule: str, message: str) -> None:
        out.append(Violation(rule, s.step, message))

    body, subject = s.body, s.subject
    both = subject + "\n" + body
    if not body.strip():
        v("empty_body", "The email body is empty.")

    n = len(words(body))
    cap = WORD_CAPS.get(s.step)
    if cap and n > cap:
        v("word_cap", f"Email {s.step} has {n} words; the limit is {cap}.")

    out += _check_subject(s, ctx)

    if "!" in both:
        v("exclamation", "Remove the exclamation mark.")
    if _HTML.search(both):
        v("html", "Plain text only: remove the HTML.")
    tags = sorted({m for rx in _MERGE_TAGS for m in rx.findall(both)})
    if tags:
        v("merge_tag", f"Unfilled placeholder(s): {', '.join(tags)}.")
    hits = find_phrases(both, banned)
    if hits:
        v("banned_phrase", f"Banned phrase(s): {', '.join(repr(h) for h in hits)}.")

    if s.step == 1:
        opener = _opening_word(body, ctx.lead_first_name)
        if opener in OPENER_WORDS:
            v("email1_opener", f"Email 1 starts with {opener!r}: open with them, not us.")
        questions = len(re.findall(r"\?+", body))
        if questions != 1:
            v("email1_questions", f"Email 1 needs exactly one question; it has {questions}.")
        if find_urls(body):
            v("email1_link", "Email 1 can't contain a link.")

    # reference policy
    platforms = find_phrases(both, PLATFORM_WORDS) + (["r/…"] if re.search(r"(?<!\w)r/\w+", both) else [])
    if platforms:
        v("platform_mention", f"Don't mention {', '.join(platforms)}: say the theme, not where it was seen.")
    if not ctx.is_signal_author:
        hits = find_phrases(both, YOUR_POST_PHRASES)
        if hits:
            v("your_post", f"{hits[0]!r} is only allowed when this lead wrote the signal.")
    if ctx.reference_policy == "theme_only":
        hits = find_phrases(both, SOURCE_WORDS)
        if hits:
            v("source_mention", f"theme_only: mention the pain theme only, not the source ({hits[0]!r}).")
    if s.step >= 2:
        hits = find_phrases(both, TIME_WORDS)
        if hits:
            v("time_word", f"Email {s.step} is sent days later, so {hits[0]!r} won't be true.")

    out += _check_claims(s, ctx)

    quote = _long_quote(both, ctx.signal_texts)
    if quote:
        v("long_quote", f"Quotes more than {MAX_QUOTE_WORDS} words of the signal verbatim: "
                        f"\"{' '.join(quote[:8])}…\". Paraphrase it.")
    return out


def _check_subject(s: Step, ctx: Context) -> list[Violation]:
    out: list[Violation] = []
    subject = s.subject.strip()
    if not subject and s.step > 1:
        return out  # follow-ups may stay in the same thread
    n = len(words(subject))
    lo, hi = SUBJECT_WORDS
    if not lo <= n <= hi:
        out.append(Violation("subject_words", s.step, f"Subject has {n} words; use {lo}-{hi}."))
    # The company's own spelling ("Acme website") is allowed; everything else is lowercase.
    rest = _without(subject, ctx.company)
    if rest != rest.lower():
        out.append(Violation("subject_case", s.step, "Subject must be lowercase."))
    names = [x for x in (ctx.lead_full_name, ctx.lead_first_name) if x and x.strip()]
    if find_phrases(subject, names):
        out.append(Violation("subject_name", s.step, "Don't put the lead's name in the subject."))
    if find_phrases(subject, ["quick question"]):
        out.append(Violation("subject_quick_question", s.step, "Don't use that subject line."))
    if _FAKE_REPLY.match(subject):
        out.append(Violation("subject_fake_reply", s.step,
                             "No Re:/Fwd: on a thread that doesn't exist (CAN-SPAM)."))
    return out


def _check_claims(s: Step, ctx: Context) -> list[Violation]:
    """Deterministic proxy for "every claim traces to a proof": numbers and client names."""
    out: list[Violation] = []
    proofs = " \n ".join(ctx.proof_quotes)
    text = _without(s.subject + "\n" + s.body, ctx.lead_full_name, ctx.lead_first_name, ctx.company)
    unproven = sorted(_numbers(text) - _numbers(proofs))
    if unproven:
        out.append(Violation("claim_unproven", s.step,
                             f"Number(s) not found in any proof: {', '.join(unproven)}."))
    proof_norm = _norm(proofs)
    names = [n for n in find_phrases(text, ctx.claimable_names) if _norm(n) not in proof_norm]
    if names:
        out.append(Violation("claim_unproven", s.step,
                             f"Name(s) used as a claim without a proof: {', '.join(names)}."))
    return out


def _long_quote(text: str, signals: list[str]) -> tuple[str, ...] | None:
    n = MAX_QUOTE_WORDS + 1
    mine = _ngrams(text, n)
    if not mine:
        return None
    for sig in signals:
        common = mine & _ngrams(sig, n)
        if common:
            return min(common)
    return None


def _check_sequence_links(steps: list[Step], ctx: Context) -> list[Violation]:
    out: list[Violation] = []
    allowed = {_url_key(u) for u in ctx.allowed_urls}
    total = 0
    for s in steps:
        urls = find_urls(s.subject + "\n" + s.body)
        total += len(urls)
        foreign = [u for u in urls if _url_key(u) not in allowed]
        if foreign:
            out.append(Violation("url_not_allowed", s.step,
                                 f"Link(s) not on the allowlist: {', '.join(foreign)}."))
    if total > MAX_LINKS_PER_SEQUENCE:
        out.append(Violation("link_count", None,
                             f"{total} links across the sequence; at most {MAX_LINKS_PER_SEQUENCE}."))
    return out


def _check_recipient(ctx: Context) -> list[Violation]:
    a, b = ctx.recipient_email.strip().lower(), ctx.db_recipient_email.strip().lower()
    if not a or a != b or "@" not in a:
        return [Violation("recipient_mismatch", None,
                          f"Recipient {ctx.recipient_email!r} doesn't match the lead record "
                          f"{ctx.db_recipient_email!r}.")]
    return []


def _check_footer(ctx: Context) -> list[Violation]:
    out: list[Violation] = []
    missing = [label for label, value in (("sender name", ctx.sender_name),
                                          ("postal address", ctx.postal_address),
                                          ("opt-out line", ctx.opt_out_line))
               if not (value or "").strip()]
    if missing:
        out.append(Violation("footer_missing", None,
                             f"Footer is missing the {' and '.join(missing)}; the send is held "
                             "until it is set in product.yaml voice."))
    line = (ctx.opt_out_line or "").strip()
    if line:
        low = line.lower()
        if find_urls(line) or "@" in line or "http" in low or "click" in low:
            out.append(Violation("opt_out_link", None,
                                 "The opt-out line can't contain a link or an email address: "
                                 "opting out must be as simple as replying."))
        if not (is_opt_out_phrase(line) and re.search(r"\breply\b", low)):
            out.append(Violation("opt_out_keyword", None,
                                 "The opt-out line must ask for a reply the handler recognises, "
                                 'e.g. Reply "no thanks". Recognised: '
                                 + ", ".join(f'"{k}"' for k in OPT_OUT_KEYWORDS) + "."))
    if ctx.notice is not None and not ctx.notice.strip():
        out.append(Violation("notice_missing", None,
                             "This recipient's country requires a notice, but no notice text is set."))
    return out
