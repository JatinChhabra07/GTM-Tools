"""Free company-website profiler (plan §5 Phase 2/3; adapted from Harvey's collectors/profile.py, MIT).

One prospect domain in, a `Profile` out: homepage, robots.txt, sitemap and a handful of linked pages
(about/team, careers, contact, pricing, customers), fetched only through the SSRF-safe
`PublicClient`. Everything is read from HTML we already hold, with no browser and no model.

Harvey ideas kept, with B2B word lists: a name only counts when a title follows it, script/style
text is never read as prose, and tech is detected from markup rather than copy. Unlike Harvey,
failures become findings (`SITE_UNREACHABLE`, `PROFILE_FAILED`) instead of silence.

The caller decides what to store: this module returns findings for every code it can see and
leaves the "only confirmed codes" filter to the observation layer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from gtm.public_http import Page, PublicClient, PublicHTTPError
from gtm.sources.snov import GENERIC_PREFIXES
from gtm.util import EMAIL_RE, domain_of

MAX_TEXT = 20_000
MAX_CHILD_SITEMAPS = 3
MAX_PEOPLE = 40
MAX_JOBS = 40


@dataclass
class Person:
    name: str
    title: str
    url: str  # page where found


@dataclass
class Finding:
    code: str  # signal code from gtm/catalog.py
    value_text: str | None
    value_num: float | None
    confidence: float  # 0..1
    evidence_url: str  # http(s) URL of the page that proves it


@dataclass
class Profile:
    domain: str
    ok: bool = False  # homepage fetched
    final_url: str | None = None
    company_name: str | None = None
    linkedin_url: str | None = None
    hq_country: str | None = None  # ISO-3166 alpha-2
    findings: list[Finding] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    generic_inboxes: list[str] = field(default_factory=list)
    printed_emails: list[str] = field(default_factory=list)
    company_phone: str | None = None  # main line from a tel: link on their own site (manual follow-up)
    company_phone_url: str | None = None
    job_titles: list[str] = field(default_factory=list)
    pages: dict[str, str] = field(default_factory=dict)  # kind -> url fetched
    page_text: dict[str, str] = field(default_factory=dict)  # url -> visible text
    errors: list[str] = field(default_factory=list)

    def add(self, code: str, *, text: str | None = None, num: float | None = None,
            confidence: float, url: str) -> None:
        if any(f.code == code and f.value_text == text and f.value_num == num for f in self.findings):
            return
        self.findings.append(Finding(code, text, num, confidence, url))

    def codes(self) -> set[str]:
        return {f.code for f in self.findings}


# ── tech fingerprints: lowercase substrings matched against markup (tags + scripts), never copy ──

TECH_SIGNATURES: dict[str, tuple[str, ...]] = {
    # commerce / CMS
    "Shopify": ("cdn.shopify.com", "myshopify.com", "shopify.theme"),
    "WooCommerce": ("woocommerce", "wc-ajax"),
    "Magento": ("data-mage-init", "mage/cookies", "magento_"),
    "BigCommerce": ("cdn11.bigcommerce.com", "bigcommerce.com/s-"),
    "WordPress": ("wp-content/", "wp-includes/"),
    "Webflow": ("website-files.com", "data-wf-site", "data-wf-page"),
    "Wix": ("static.parastorage.com", "wixstatic.com"),
    "Squarespace": ("static1.squarespace.com", "squarespace-cdn.com"),
    # marketing automation / CRM
    "HubSpot": ("js.hs-scripts.com", "js.hsforms.net", "js.hs-analytics.net", "js.hs-banner.com"),
    "Salesforce Pardot": ("pi.pardot.com", "go.pardot.com", "piaid ="),
    "Salesforce": ("service.force.com", "my.salesforce.com", "embeddedservice_bootstrap",
                   "salesforceliveagent.com"),
    "Marketo": ("munchkin.marketo.net", "mktoforms", "marketo.com/js"),
    "Klaviyo": ("static.klaviyo.com", "a.klaviyo.com", "klaviyo.com/onsite"),
    "Mailchimp": ("chimpstatic.com", "list-manage.com"),
    "CleverTap": ("clevertap",),
    "MoEngage": ("moengage",),
    "WebEngage": ("webengage",),
    "Netcore": ("netcoresmartech", "smartech-api"),
    # chat / support
    "Intercom": ("widget.intercom.io", "js.intercomcdn.com"),
    "Drift": ("js.driftt.com", "js.drift.com"),
    "Zendesk": ("static.zdassets.com", "zopim.com"),
    "Freshdesk": ("freshdesk.com", "widget.freshworks.com"),
    "Freshchat": ("wchat.freshchat.com", "freshchat.com/js"),
    "Zoho SalesIQ": ("salesiq.zoho", "salesiq.zohopublic"),
    "Gorgias": ("gorgias.chat", "gorgias.io"),
    "Tawk.to": ("embed.tawk.to",),
    # analytics / testing
    "Segment": ("cdn.segment.com",),
    "Mixpanel": ("cdn.mxpnl.com", "mixpanel.init"),
    "Amplitude": ("cdn.amplitude.com", "amplitude.getinstance"),
    "Hotjar": ("static.hotjar.com",),
    "Microsoft Clarity": ("clarity.ms/tag",),
    "VWO": ("visualwebsiteoptimizer.com", "_vwo_code"),
    "Google Analytics": ("google-analytics.com/analytics.js", "gtag/js?id=g-", "gtag/js?id=ua-",
                         "google-analytics.com/ga.js"),
    "Google Tag Manager": ("googletagmanager.com/gtm.js", "googletagmanager.com/ns.html"),
    "LinkedIn Insight": ("snap.licdn.com", "_linkedin_partner_id"),
    "TikTok Pixel": ("analytics.tiktok.com",),
    # payments
    "Stripe": ("js.stripe.com",),
    "Razorpay": ("checkout.razorpay.com", "razorpay.com/v1"),
    "PayU": ("payu.in", "payumoney.com", "secure.payu"),
    "Cashfree": ("sdk.cashfree.com", "cashfree.com/js"),
    "Paytm Payment Gateway": ("securegw.paytm.in",),
    # e-commerce apps (mostly Shopify ecosystem / Indian D2C)
    "Yotpo": ("yotpo.com",),
    "Recharge": ("rechargecdn.com", "rechargepayments.com", "rechargeapps.com"),
    "Okendo": ("okendo.io",),
    "Judge.me": ("judge.me",),
    "GoKwik": ("gokwik.co", "gokwik"),
    "Shiprocket": ("shiprocket",),
    # scheduling / forms
    "Calendly": ("assets.calendly.com", "calendly.com/"),
    "Typeform": ("embed.typeform.com", "typeform.com/to/"),
}

# Ad tags: plain Google Analytics is not ad spend, so only conversion/remarketing tags count.
GOOGLE_ADS_RE = re.compile(r"googleadservices\.com|google_conversion|[\"'/=(]AW-\d{6,}", re.I)
META_PIXEL_RE = re.compile(r"connect\.facebook\.net/[^\"']*fbevents\.js|fbq\s*\(\s*['\"]init", re.I)

# Hosted job boards a site may link its careers page to. Fetched only for the careers kind.
ATS_HOSTS = ("greenhouse.io", "lever.co", "workable.com", "ashbyhq.com", "smartrecruiters.com",
             "keka.com", "darwinbox.in", "zohorecruit.com", "zohorecruit.in", "freshteam.com",
             "breezy.hr", "recruitee.com", "bamboohr.com", "teamtailor.com", "jobvite.com")

# kind -> slug keywords; fetch priority is this order.
PAGE_KINDS: dict[str, tuple[str, ...]] = {
    "team": ("team", "our-team", "meet-the-team", "leadership", "people", "founders", "management"),
    "about": ("about", "about-us", "company", "who-we-are", "our-story"),
    "careers": ("careers", "career", "jobs", "join-us", "work-with-us", "hiring", "openings"),
    "contact": ("contact", "contact-us", "get-in-touch"),
    "pricing": ("pricing", "plans"),
    "customers": ("customers", "case-studies", "case-study", "success-stories", "clients"),
}

# ── people / jobs word lists ──

TITLE_WORD_RE = re.compile(
    r"\b(ceo|cto|cmo|coo|cfo|cro|cpo|ciso|cio|founder|co-?founder|president|head|vp|svp|evp|"
    r"vice president|director|manager|lead|partner|chief|officer|principal|owner|chair(?:man|person)?|"
    r"managing|md)\b", re.I)
ROLE_WORD_RE = re.compile(
    r"\b(manager|engineer|developer|head|lead|director|executive|specialist|marketer|analyst|"
    r"designer|associate|intern|officer|vp|sales|growth|consultant|coordinator|representative|"
    r"architect|scientist|writer|strategist|administrator|accountant|recruiter|sdr|bdr|"
    r"account|devops|qa|tester|copywriter|editor|programmer|product owner)\b", re.I)
_CONNECTORS = frozenset("of and the for & to in at - – — / , (remote) remote".split())
_JOB_LEADS = frozenset(
    "apply view see join open our why life careers jobs explore browse meet about learn read back "
    "all current no".split())

# Words that are never part of a personal name. Without them, nav and section headings like
# "Our Team", "Read More" or "Customer Success" pass a Firstname-Lastname test.
NON_NAME_WORDS = frozenset("""
our the your their my we us team teams about contact read more learn view all home blog careers
career jobs pricing plans customers customer case studies study product products platform
solutions solution services service features feature resources resource company leadership
people meet founders investors partners partner news press events event login log sign up get
started book demo request free trial privacy policy terms cookie cookies support help docs
documentation integrations integration api security enterprise startup startups why open
positions join work with culture values mission vision story board advisors advisory management
executive office offices global success stories success engineering marketing sales growth
operations design data cloud software analytics commerce retail technology tech digital
industries industry agency studio labs capital ventures group private limited ltd pvt inc llc
corp corporation holdings technologies systems network networks media health care finance
head chief director manager lead founder officer president principal owner vice our
india bengaluru bangalore mumbai delhi new york london san francisco singapore dubai
apply now today watch download subscribe follow next previous menu close search skip main
""".split())
_NOUN_SUFFIX = re.compile(r"(tion|tions|sion|sions|ness|ment|ments|ology|ware)$")
_NAME_TOKEN = re.compile(r"^(?:[A-Z][a-zà-ÿ]+(?:['’-][A-Za-zà-ÿ]+)*|[A-Z]\.)$")
_NAME_THEN_TITLE = re.compile(
    r"^(?P<name>[^,|:–—·-]{3,60}?)\s*(?:,|\||:|–|—|·|\s-\s)\s*(?P<title>.+)$")

COUNTRY_NAMES = {
    "india": "IN", "united states": "US", "united states of america": "US", "usa": "US",
    "u.s.a.": "US", "us": "US", "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "england": "GB", "canada": "CA", "australia": "AU", "germany": "DE", "france": "FR",
    "singapore": "SG", "united arab emirates": "AE", "uae": "AE", "netherlands": "NL",
    "ireland": "IE", "spain": "ES", "italy": "IT", "sweden": "SE", "israel": "IL",
    "new zealand": "NZ", "japan": "JP", "indonesia": "ID", "malaysia": "MY",
    "saudi arabia": "SA", "south africa": "ZA", "brazil": "BR", "switzerland": "CH",
}
SIZE_BANDS = ((10, "1-10"), (50, "11-50"), (200, "51-200"), (500, "201-500"),
              (1000, "501-1000"), (5000, "1001-5000"))


# ── HTML parsing ──

_SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
              "track", "wbr"}
_JOB_CONTEXT = {"h2", "h3", "h4", "h5", "h6", "a", "li", "dt"}


@dataclass
class _Doc:
    url: str
    markup: str  # tags and script bodies, lowercased: where tech lives
    text: str  # visible text
    segments: list[tuple[str, frozenset[str]]]  # (text node, enclosing tags) in document order
    links: list[tuple[str, str]]  # (absolute url, anchor text)
    jsonld: list[str]
    title: str
    og_site_name: str


class _Parser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.stack: list[str] = []
        self.markup: list[str] = []
        self.text: list[str] = []
        self.segments: list[tuple[str, frozenset[str]]] = []
        self.links: list[tuple[str, str]] = []
        self.jsonld: list[str] = []
        self.title = ""
        self.og_site_name = ""
        self._link: tuple[str, list[str]] | None = None
        self._ld = False

    def handle_starttag(self, tag, attrs):
        self.markup.append(self.get_starttag_text() or "")
        a = {k: (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self._link = (a["href"], [])
        elif tag == "meta" and a.get("property", "").lower() == "og:site_name":
            self.og_site_name = a.get("content", "").strip()
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._ld = True
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.markup.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag):
        if tag == "a" and self._link is not None:
            href, parts = self._link
            self.links.append((urljoin(self.base_url, href.strip()), " ".join(parts).strip()))
            self._link = None
        if tag == "script":
            self._ld = False
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data):
        if self._skipping():
            self.markup.append(data)
            if self._ld:
                self.jsonld.append(data)
            return
        clean = re.sub(r"\s+", " ", data).strip()
        if not clean:
            return
        if "title" in self.stack and not self.title:
            self.title = clean
            return
        if "head" in self.stack:
            return
        self.text.append(clean)
        self.segments.append((clean, frozenset(self.stack)))
        if self._link is not None:
            self._link[1].append(clean)

    def handle_comment(self, data):
        self.markup.append(data)

    def _skipping(self) -> bool:
        return any(t in _SKIP_TAGS for t in self.stack)


def _parse(url: str, html: str) -> _Doc:
    p = _Parser(url)
    try:
        p.feed(html)
        p.close()
    except (ValueError, AssertionError):  # malformed markup: keep what was parsed so far
        pass
    links = [(urldefrag(u)[0], t) for u, t in p.links]
    return _Doc(url=url, markup="\n".join(p.markup).lower(), text=" ".join(p.text)[:MAX_TEXT],
                segments=p.segments, links=links, jsonld=p.jsonld, title=p.title,
                og_site_name=p.og_site_name)


# ── URL helpers ──

def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _on_site(url: str, domain: str) -> bool:
    host = _host(url)
    return urlparse(url).scheme in ("http", "https") and (host == domain or host.endswith("." + domain))


def _is_ats(url: str) -> bool:
    host = _host(url)
    return urlparse(url).scheme in ("http", "https") and any(
        host == h or host.endswith("." + h) for h in ATS_HOSTS)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _kind_of(url: str, anchor: str = "") -> str | None:
    last = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    last = re.sub(r"\.(html?|php|aspx?)$", "", last.lower())
    for slug in (last, _slug(anchor)):
        for kind, words in PAGE_KINDS.items():
            if slug in words:
                return kind
    return None


def _discover(links, domain: str, home_url: str, candidates: dict[str, str]) -> None:
    for url, anchor in links:
        if url.rstrip("/") == home_url.rstrip("/"):
            continue
        if _on_site(url, domain):
            kind = _kind_of(url, anchor)
        elif _is_ats(url):
            kind = "careers"  # any link to their job board is the careers page
        else:
            continue
        if kind:
            candidates.setdefault(kind, url)


# ── fetching ──

_FETCH_ERRORS = (PublicHTTPError, httpx.HTTPError, OSError, ValueError, UnicodeError)


def _fetch(client, url: str) -> tuple[Page | None, str | None]:
    try:
        page = client.get(url)
    except _FETCH_ERRORS as e:
        return None, f"{type(e).__name__}: {e}"[:200]
    if not 200 <= page.status < 300:
        return None, f"HTTP {page.status}"
    return page, None


def _fetch_home(client, domain: str, prof: Profile) -> Page | None:
    # Many small sites have no TLS cert or only answer on www, so a single https guess would
    # mark live sites unreachable.
    tried = [f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"]
    reason = ""
    for url in tried:
        page, err = _fetch(client, url)
        if page is not None:
            if _on_site(page.url, domain):
                return page
            err = f"redirected off-site to {_host(page.url)}"
        reason = err or reason
        prof.errors.append(f"{url}: {err}")
    prof.add("SITE_UNREACHABLE", text=reason[:200], confidence=1.0, url=tried[0])
    return None


def _sitemap(client, base: str, domain: str, prof: Profile) -> list[str]:
    """Return the page URLs listed in the site's sitemap(s); records SITE_PAGE_COUNT."""
    robots, _ = _fetch(client, f"{base}/robots.txt")
    declared = []
    if robots is not None:
        declared = [m.strip() for m in re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots.text)]
    roots = [u for u in declared if _on_site(u, domain)][:MAX_CHILD_SITEMAPS] or [f"{base}/sitemap.xml"]
    locs: list[str] = []
    evidence = ""
    partial = False
    for root in roots:
        page, err = _fetch(client, root)
        if page is None:
            prof.errors.append(f"sitemap {root}: {err}")
            continue
        found = _locs(page.text)
        if "<sitemapindex" in page.text.lower():
            children = [u for u in found if _on_site(u, domain) and not u.endswith(".gz")]
            partial = partial or len(children) > MAX_CHILD_SITEMAPS
            for child in children[:MAX_CHILD_SITEMAPS]:
                cpage, cerr = _fetch(client, child)
                if cpage is None:
                    prof.errors.append(f"sitemap {child}: {cerr}")
                    continue
                locs += _locs(cpage.text)
        else:
            locs += found
        evidence = evidence or page.url
    locs = list(dict.fromkeys(locs))
    if locs:
        prof.add("SITE_PAGE_COUNT", num=float(len(locs)), confidence=0.7 if partial else 0.9,
                 text="lower bound: sitemap index truncated" if partial else None, url=evidence)
    return locs


def _locs(xml: str) -> list[str]:
    return [m.strip() for m in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", xml, re.I)]


# ── detectors ──

def _detect_tech(doc: _Doc, prof: Profile) -> None:
    for name, needles in TECH_SIGNATURES.items():
        if any(n in doc.markup for n in needles):
            prof.add("TECH_STACK", text=name, confidence=0.9, url=doc.url)
    if GOOGLE_ADS_RE.search(doc.markup):
        prof.add("RUNNING_GOOGLE_ADS", num=1.0, confidence=0.9, url=doc.url)
    if META_PIXEL_RE.search(doc.markup):
        prof.add("RUNNING_META_ADS", num=1.0, confidence=0.9, url=doc.url)


def _reads_like_title(text: str, word_re: re.Pattern) -> bool:
    """Short, mostly capitalised, with a role word. "We help founders scale" is copy, not a title."""
    words = text.split()
    if not 1 <= len(words) <= 10 or text.endswith((".", "!", "?")):
        return False
    lower = [w for w in words[1:] if w[:1].islower() and w.lower() not in _CONNECTORS]
    return len(lower) <= 2 and bool(word_re.search(text))


def _is_name(text: str, banned: set[str]) -> bool:
    tokens = text.split()
    if not 2 <= len(tokens) <= 4 or len(text) > 45:
        return False
    if not all(_NAME_TOKEN.match(t) for t in tokens) or tokens[0].endswith(".") or tokens[-1].endswith("."):
        return False
    for t in tokens:
        low = t.lower().strip(".'’")
        if low in NON_NAME_WORDS or low in banned or _NOUN_SUFFIX.search(low):
            return False
    return True


def _clean_title(text: str) -> str:
    return text.strip(" ,.|:-–—·").strip()[:80]


def _detect_people(doc: _Doc, banned: set[str]) -> list[Person]:
    people: list[Person] = []
    segs = [s for s, _ in doc.segments]
    for i, seg in enumerate(segs):
        name, title = None, None
        if _is_name(seg, banned):
            # Title in the next text node (next element, or the rest of the same element).
            name, title = seg, _clean_title(segs[i + 1]) if i + 1 < len(segs) else ""
        else:
            m = _NAME_THEN_TITLE.match(seg)
            if m and _is_name(m.group("name").strip(), banned):
                name, title = m.group("name").strip(), _clean_title(m.group("title"))
        if not name or not title or not _reads_like_title(title, TITLE_WORD_RE):
            continue
        if _is_name(title, banned):
            continue
        people.append(Person(name=name, title=title, url=doc.url))
    return people


def _detect_jobs(doc: _Doc) -> list[str]:
    jobs: list[str] = []
    for seg, tags in doc.segments:
        if not tags & _JOB_CONTEXT:
            continue
        title = _clean_title(seg)
        words = title.split()
        if not 2 <= len(words) <= 10 or words[0].lower() in _JOB_LEADS:
            continue
        if _reads_like_title(title, ROLE_WORD_RE) and title.lower() not in {j.lower() for j in jobs}:
            jobs.append(title)
    return jobs[:MAX_JOBS]


_AT_RE = re.compile(r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\})\s*", re.I)
_DOT_RE = re.compile(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\})\s*", re.I)


def _emails(doc: _Doc) -> list[str]:
    found = []
    for url, _ in doc.links:
        if url.lower().startswith("mailto:"):
            found += EMAIL_RE.findall(url[7:].split("?", 1)[0])
    found += EMAIL_RE.findall(_DOT_RE.sub(".", _AT_RE.sub("@", doc.text)))
    return [e.lower().strip(".") for e in found]


def _tel(url: str) -> str | None:
    """A tel: link's number, normalised (+ and digits), if it is a plausible phone number."""
    if not url.lower().startswith("tel:"):
        return None
    raw = re.sub(r"[^\d+]", "", url[4:].split("?", 1)[0])
    digits = raw.lstrip("+")
    return raw if 7 <= len(digits) <= 15 and digits.isdigit() and "+" not in digits else None


def _same_domain_email(email: str, domain: str) -> bool:
    host = email.rsplit("@", 1)[-1]
    return host == domain or host.endswith("." + domain)


# ── schema.org / naming ──

def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _is_org(node: dict) -> bool:
    types = node.get("@type", [])
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and (t.endswith("Organization") or t.endswith("Business")
                                       or t in ("Corporation", "OnlineStore", "Store"))
               for t in types)


def _orgs(doc: _Doc) -> list[dict]:
    out = []
    for blob in doc.jsonld:
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        out += [n for n in _walk(data) if _is_org(n)]
    return out


def _country_code(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("identifier")
    if not isinstance(value, str):
        return None
    v = value.strip()
    if re.fullmatch(r"[A-Za-z]{2}", v):
        return "GB" if v.upper() == "UK" else v.upper()
    return COUNTRY_NAMES.get(v.lower())


def _employee_count(value) -> int | None:
    if isinstance(value, dict):
        value = value.get("value") or value.get("maxValue") or value.get("minValue")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        nums = [int(n) for n in re.findall(r"\d+", value.replace(",", ""))]
        return max(nums) if nums else None
    return None


def size_band(n: int) -> str:
    for upper, band in SIZE_BANDS:
        if n <= upper:
            return band
    return "5001+"


def _linkedin(url: str) -> str | None:
    m = re.match(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([^/?#]+)", url.strip(), re.I)
    return f"https://www.linkedin.com/company/{m.group(1)}" if m else None


def _name_from_title(title: str, domain: str) -> str | None:
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
    parts = [p.strip() for p in re.split(r"\s+[–—:-]\s+|\|", title) if p.strip()]
    for p in parts:
        if label and label in re.sub(r"[^a-z0-9]", "", p.lower()):
            return p[:80]
    return parts[-1][:80] if len(parts) > 1 else (parts[0][:80] if parts else None)


def _apply_schema(doc: _Doc, prof: Profile) -> None:
    for org in _orgs(doc):
        if not prof.company_name and isinstance(org.get("name"), str):
            prof.company_name = org["name"].strip()[:80] or None
        same = org.get("sameAs", [])
        for u in same if isinstance(same, list) else [same]:
            if isinstance(u, str) and not prof.linkedin_url:
                prof.linkedin_url = _linkedin(u)
        addresses = org.get("address", [])
        for addr in addresses if isinstance(addresses, list) else [addresses]:
            code = _country_code(addr.get("addressCountry")) if isinstance(addr, dict) else None
            if code and not prof.hq_country:
                prof.hq_country = code
                prof.add("HQ_COUNTRY", text=code, confidence=0.7, url=doc.url)
        n = _employee_count(org.get("numberOfEmployees"))
        if n and n > 0:
            prof.add("SIZE_BAND", text=size_band(n), confidence=0.6, url=doc.url)


# ── the profiler ──

def profile(domain: str, *, client=None, hiring_keywords: list[str] = (), competitors: list[str] = (),
            max_pages: int = 8) -> Profile:
    """Profile one company website. Never raises for network or HTML problems.

    `client` is anything with `.get(url) -> Page` (a `PublicClient` by default; tests pass a stub).
    `max_pages` caps HTML pages fetched (homepage included); robots.txt and sitemaps are extra.
    `competitors` is accepted for the caller's future use and currently ignored: a competitor's
    script on the site already shows up as a TECH_STACK finding and the caller decides what it
    means; a competitor named in customer-page copy says nothing about this account.
    """
    del competitors
    prof = Profile(domain=domain_of(domain))
    if not prof.domain:
        prof.errors.append("no domain")
        return prof
    own = client is None
    client = client or PublicClient()
    try:
        _run(prof, client, [k.lower() for k in hiring_keywords if k], max_pages)
    finally:
        if own:
            client.close()
    return prof


def _run(prof: Profile, client, keywords: list[str], max_pages: int) -> None:
    domain = prof.domain
    home = _fetch_home(client, domain, prof)
    if home is None:
        return
    prof.ok, prof.final_url = True, home.url
    parsed = urlparse(home.url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    home_doc = _parse(home.url, home.text)
    docs: list[tuple[str, _Doc]] = [("home", home_doc)]
    prof.pages["home"] = home.url
    candidates: dict[str, str] = {}
    _discover(home_doc.links, domain, home.url, candidates)
    for loc in _sitemap(client, base, domain, prof):
        if _on_site(loc, domain) and (kind := _kind_of(loc)):
            candidates.setdefault(kind, loc)

    by_url = {home.url: home_doc}
    pending = list(PAGE_KINDS)
    budget = max_pages - 1
    while budget > 0 and (kind := next((k for k in pending if k in candidates), None)):
        pending.remove(kind)
        url = candidates[kind]
        if url in by_url:
            prof.pages[kind] = url
            docs.append((kind, by_url[url]))
            continue
        budget -= 1
        page, err = _fetch(client, url)
        if page is not None and not (_on_site(page.url, domain) or (kind == "careers" and _is_ats(page.url))):
            page, err = None, f"redirected off-site to {_host(page.url)}"
        if page is None:
            prof.errors.append(f"{kind} {url}: {err}")
            prof.add("PROFILE_FAILED", text=f"{kind} page: {err}"[:200], confidence=1.0, url=url)
            continue
        doc = _parse(page.url, page.text)
        by_url[url] = by_url[page.url] = doc
        prof.pages[kind] = page.url
        docs.append((kind, doc))
        _discover(doc.links, domain, home.url, candidates)

    _collect(prof, docs, keywords)


def _collect(prof: Profile, docs: list[tuple[str, _Doc]], keywords: list[str]) -> None:
    domain = prof.domain
    home_doc = docs[0][1]
    _apply_schema(home_doc, prof)
    if home_doc.og_site_name:
        prof.company_name = home_doc.og_site_name[:80]
    prof.company_name = prof.company_name or _name_from_title(home_doc.title, domain)
    banned = {w.lower() for w in re.findall(r"[A-Za-z]+", prof.company_name or "")}
    banned.add(domain.split(".")[0])

    seen_docs: set[int] = set()
    for kind, doc in docs:
        if id(doc) in seen_docs:
            continue
        seen_docs.add(id(doc))
        prof.page_text[doc.url] = doc.text
        _detect_tech(doc, prof)
        if kind != "home":
            _apply_schema(doc, prof)
        for url, _ in doc.links:
            if not prof.linkedin_url and (li := _linkedin(url)):
                prof.linkedin_url = li
            if kind in ("home", "contact", "about") and not prof.company_phone and (tel := _tel(url)):
                prof.company_phone, prof.company_phone_url = tel, doc.url
        for email in _emails(doc):
            if not _same_domain_email(email, domain) or email in prof.printed_emails:
                continue
            prof.printed_emails.append(email)
            if email.split("@", 1)[0] in GENERIC_PREFIXES:
                prof.generic_inboxes.append(email)
                prof.add("GENERIC_INBOX", text=email, confidence=0.9, url=doc.url)
        if kind in ("team", "about"):
            for person in _detect_people(doc, banned):
                if len(prof.people) >= MAX_PEOPLE or any(p.name == person.name for p in prof.people):
                    continue
                prof.people.append(person)
                prof.add("TEAM_MEMBER_FOUND", text=f"{person.name} | {person.title}",
                         confidence=0.8, url=doc.url)
        if kind == "careers":
            for title in _detect_jobs(doc):
                if title.lower() in {j.lower() for j in prof.job_titles}:
                    continue
                prof.job_titles.append(title)
                prof.add("HIRING_ROLE", text=title, confidence=0.8, url=doc.url)
                if any(k in title.lower() for k in keywords):
                    prof.add("HIRING_FOR_PROBLEM", text=title, confidence=0.7, url=doc.url)
