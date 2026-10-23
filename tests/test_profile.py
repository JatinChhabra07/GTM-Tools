"""Website profiler: a stub client serves fixture HTML, so nothing here touches the network."""

from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from gtm.catalog import CATALOG
from gtm.profile import TECH_SIGNATURES, Profile, profile, size_band
from gtm.public_http import Page, PublicHTTPError

FIX = Path(__file__).parent / "fixtures" / "profile"
SITE = "https://acmeretail.in"

ROBOTS = """User-agent: *
Disallow: /checkout
Sitemap: https://acmeretail.in/sitemap_index.xml
Sitemap: https://othersite.com/sitemap.xml
"""
SITEMAP_INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://acmeretail.in/sitemap_pages_1.xml</loc></sitemap>
  <sitemap><loc>https://acmeretail.in/sitemap_products_1.xml</loc></sitemap>
  <sitemap><loc>https://cdn.elsewhere.com/sitemap.xml</loc></sitemap>
</sitemapindex>"""
SITEMAP_PAGES = """<urlset>
  <url><loc>https://acmeretail.in/</loc></url>
  <url><loc>https://acmeretail.in/pages/about-us</loc></url>
  <url><loc>https://acmeretail.in/pages/pricing</loc></url>
</urlset>"""
SITEMAP_PRODUCTS = """<urlset>
  <url><loc><![CDATA[https://acmeretail.in/products/face-wash]]></loc></url>
  <url><loc>https://acmeretail.in/products/serum</loc></url>
</urlset>"""
PRICING = "<html><body><h1>Pricing</h1><p>Plans from Rs 499.</p></body></html>"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class StubClient:
    """Anything with .get(url) -> Page. A route value is HTML, (status, body), or an exception."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str) -> Page:
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            return Page(url=url, status=404, content_type="text/html", text="not found", headers={})
        if isinstance(route, Exception):
            raise route
        status, body = route if isinstance(route, tuple) else (200, route)
        return Page(url=url, status=status, content_type="text/html", text=body, headers={})


def acme_routes(**overrides) -> dict:
    routes = {
        f"{SITE}/": fixture("home.html"),
        f"{SITE}/robots.txt": ROBOTS,
        f"{SITE}/sitemap_index.xml": SITEMAP_INDEX,
        f"{SITE}/sitemap_pages_1.xml": SITEMAP_PAGES,
        f"{SITE}/sitemap_products_1.xml": SITEMAP_PRODUCTS,
        f"{SITE}/pages/about-us": fixture("about.html"),
        f"{SITE}/team": fixture("team.html"),
        f"{SITE}/careers": fixture("careers.html"),
        f"{SITE}/contact": fixture("contact.html"),
        f"{SITE}/pages/pricing": PRICING,
    }
    routes.update(overrides)
    return routes


def run(routes=None, **kw) -> tuple[Profile, StubClient]:
    client = StubClient(routes if routes is not None else acme_routes())
    return profile("acmeretail.in", client=client, **kw), client


def values(prof: Profile, code: str) -> list:
    return [f.value_text if f.value_text is not None else f.value_num
            for f in prof.findings if f.code == code]


@pytest.fixture(scope="module")
def acme():
    client = StubClient(acme_routes())
    prof = profile("https://www.AcmeRetail.in/", client=client,
                   hiring_keywords=["performance marketing", "GROWTH"])
    return prof, client


def test_tech_signatures_cover_the_b2b_list():
    assert len(TECH_SIGNATURES) >= 35
    for tool in ("Shopify", "Razorpay", "CleverTap", "MoEngage", "WebEngage", "Netcore", "Zoho SalesIQ",
                 "Klaviyo", "HubSpot", "Google Tag Manager", "LinkedIn Insight", "Gorgias", "Typeform"):
        assert tool in TECH_SIGNATURES
    for needles in TECH_SIGNATURES.values():
        assert needles and all(n == n.lower() for n in needles)


def test_homepage_tech_and_ad_pixels(acme):
    prof, _ = acme
    assert prof.ok and prof.domain == "acmeretail.in" and prof.final_url == f"{SITE}/"
    tech = values(prof, "TECH_STACK")
    assert {"Shopify", "Klaviyo"} <= set(tech)
    # Named only in body copy, not loaded: not their stack.
    assert "Magento" not in tech and "WooCommerce" not in tech
    assert values(prof, "RUNNING_GOOGLE_ADS") == [1.0]
    assert values(prof, "RUNNING_META_ADS") == [1.0]
    shopify = next(f for f in prof.findings if f.value_text == "Shopify")
    assert shopify.confidence == 0.9 and shopify.evidence_url == f"{SITE}/"


def test_ga_only_page_is_not_ad_spend():
    prof = profile("betalabs.io", client=StubClient({"https://betalabs.io/": fixture("ga_only.html")}))
    assert prof.ok
    assert {"Google Analytics", "Google Tag Manager"} <= set(values(prof, "TECH_STACK"))
    assert "RUNNING_GOOGLE_ADS" not in prof.codes()
    assert "RUNNING_META_ADS" not in prof.codes()


def test_careers_titles_and_hiring_for_problem(acme):
    prof, _ = acme
    assert prof.job_titles == ["Senior Performance Marketing Manager", "Growth Analyst",
                               "Backend Engineer (Remote)", "Customer Support Executive"]
    assert values(prof, "HIRING_ROLE") == prof.job_titles
    assert values(prof, "HIRING_FOR_PROBLEM") == ["Senior Performance Marketing Manager", "Growth Analyst"]
    role = next(f for f in prof.findings if f.code == "HIRING_ROLE")
    assert role.confidence == 0.8 and role.evidence_url == f"{SITE}/careers"
    assert prof.pages["careers"] == f"{SITE}/careers"


def test_team_page_names_need_a_following_title(acme):
    prof, _ = acme
    people = {(p.name, p.title) for p in prof.people}
    assert people == {
        ("Priya Sharma", "Co-founder & CEO"),
        ("Rahul Mehta", "VP Sales"),
        ("Anita Rao", "Head of Marketing"),
        ("Neha Iyer", "Growth Marketing Manager"),
    }
    names = {p.name for p in prof.people}
    assert "Karan Singh" not in names  # no title follows
    assert not names & {"John Doe", "Acme Retail", "Customer Success", "Read More", "Our Team",
                        "Meet The Founders"}
    assert "Priya Sharma | Co-founder & CEO" in values(prof, "TEAM_MEMBER_FOUND")
    assert all(p.url == f"{SITE}/team" for p in prof.people)


def test_emails_deobfuscated_same_domain_only(acme):
    prof, _ = acme
    assert set(prof.printed_emails) == {"hello@acmeretail.in", "priya@acmeretail.in", "sales@acmeretail.in",
                                        "support@acmeretail.in", "press@in.acmeretail.in"}
    assert set(prof.generic_inboxes) == {"hello@acmeretail.in", "sales@acmeretail.in",
                                         "support@acmeretail.in", "press@in.acmeretail.in"}
    assert set(values(prof, "GENERIC_INBOX")) == set(prof.generic_inboxes)
    assert not any("gmail" in e or "partner-agency" in e for e in prof.printed_emails)


def test_schema_org_and_naming(acme):
    prof, _ = acme
    assert prof.company_name == "Acme Retail"  # og:site_name wins
    assert prof.hq_country == "IN"
    assert values(prof, "HQ_COUNTRY") == ["IN"]
    assert values(prof, "SIZE_BAND") == ["51-200"]
    assert prof.linkedin_url == "https://www.linkedin.com/company/acme-retail"
    hq = next(f for f in prof.findings if f.code == "HQ_COUNTRY")
    assert hq.confidence == 0.7


@pytest.mark.parametrize("n,band", [(1, "1-10"), (10, "1-10"), (11, "11-50"), (200, "51-200"),
                                    (201, "201-500"), (1000, "501-1000"), (4999, "1001-5000"),
                                    (5001, "5001+")])
def test_size_bands(n, band):
    assert size_band(n) == band


def test_title_fallback_when_no_og_or_schema():
    html = "<html><head><title>Home | Zeta Logistics</title></head><body></body></html>"
    prof = profile("zetalogistics.com", client=StubClient({"https://zetalogistics.com/": html}))
    assert prof.company_name == "Zeta Logistics"


def test_sitemap_index_counts_children(acme):
    prof, client = acme
    assert values(prof, "SITE_PAGE_COUNT") == [5.0]
    count = next(f for f in prof.findings if f.code == "SITE_PAGE_COUNT")
    assert count.evidence_url == f"{SITE}/sitemap_index.xml"
    assert "https://othersite.com/sitemap.xml" not in client.calls
    assert "https://cdn.elsewhere.com/sitemap.xml" not in client.calls


def test_sitemap_fallback_without_robots():
    routes = acme_routes()
    del routes[f"{SITE}/robots.txt"]
    routes[f"{SITE}/sitemap.xml"] = SITEMAP_PAGES
    prof, _ = run(routes)
    assert values(prof, "SITE_PAGE_COUNT") == [3.0]


def test_pages_and_text(acme):
    prof, _ = acme
    assert prof.pages == {"home": f"{SITE}/", "team": f"{SITE}/team", "about": f"{SITE}/pages/about-us",
                          "careers": f"{SITE}/careers", "contact": f"{SITE}/contact",
                          "pricing": f"{SITE}/pages/pricing"}  # pricing found via the sitemap
    home_text = prof.page_text[f"{SITE}/"]
    assert "Skincare that works" in home_text
    assert "gtag" not in home_text and "fbq" not in home_text and "color: red" not in home_text
    assert "Clean skincare" not in home_text  # <title> is head, not body copy
    assert all(len(t) <= 20_000 for t in prof.page_text.values())


def test_never_fetches_off_domain(acme):
    _, client = acme
    hosts = {urlparse(u).hostname for u in client.calls}
    assert hosts == {"acmeretail.in"}


def test_ats_careers_link_is_followed():
    home = fixture("home.html").replace('href="/careers"', 'href="https://jobs.lever.co/acmeretail"')
    routes = acme_routes(**{f"{SITE}/": home, "https://jobs.lever.co/acmeretail": fixture("lever_jobs.html")})
    prof, client = run(routes)
    assert "https://jobs.lever.co/acmeretail" in client.calls
    assert prof.pages["careers"] == "https://jobs.lever.co/acmeretail"
    assert prof.job_titles == ["Sales Development Representative", "Product Designer"]
    assert {urlparse(u).hostname for u in client.calls} == {"acmeretail.in", "jobs.lever.co"}


def test_unreachable_homepage_is_an_observation():
    err = PublicHTTPError("could not connect to acmeretail.in")
    routes = {f"{SITE}/": err, "https://www.acmeretail.in/": httpx.ConnectError("refused"),
              "http://acmeretail.in/": (503, "down")}
    prof, client = run(routes)
    assert prof.ok is False and prof.final_url is None
    assert [f.code for f in prof.findings] == ["SITE_UNREACHABLE"]
    finding = prof.findings[0]
    assert finding.evidence_url == f"{SITE}/" and finding.value_text == "HTTP 503"
    assert len(prof.errors) == 3
    assert len(client.calls) == 3


def test_http_fallback_when_https_fails():
    routes = {f"{SITE}/": PublicHTTPError("tls"), "http://acmeretail.in/": fixture("ga_only.html")}
    prof, _ = run(routes)
    assert prof.ok and prof.final_url == "http://acmeretail.in/"
    assert "SITE_UNREACHABLE" not in prof.codes()


def test_subpage_error_is_recorded_and_profiling_continues():
    prof, _ = run(acme_routes(**{f"{SITE}/team": PublicHTTPError("read timed out"),
                                 f"{SITE}/contact": (500, "oops")}))
    assert prof.ok
    failed = [f for f in prof.findings if f.code == "PROFILE_FAILED"]
    assert {f.evidence_url for f in failed} == {f"{SITE}/team", f"{SITE}/contact"}
    assert any("HTTP 500" in f.value_text for f in failed)
    assert "team" not in prof.pages and prof.people == []
    assert prof.job_titles  # careers still parsed


def test_max_pages_caps_html_fetches():
    prof, client = run(max_pages=2)
    html_calls = [u for u in client.calls if not u.endswith((".xml", ".txt"))]
    assert len(html_calls) == 2
    assert set(prof.pages) == {"home", "team"}


def test_off_site_redirect_is_rejected():
    class Redirecting(StubClient):
        def get(self, url):
            page = super().get(url)
            if url == f"{SITE}/contact":
                page.url = "https://evil.example.com/contact"
            return page

    prof = profile("acmeretail.in", client=Redirecting(acme_routes()))
    assert "contact" not in prof.pages
    assert any(f.code == "PROFILE_FAILED" and "off-site" in f.value_text for f in prof.findings)


def test_garbage_html_never_raises():
    junk = "<html><body><<<>>> <a href='javascript:alert(1)'>x</a> <script>unterminated"
    prof = profile("junk.io", client=StubClient({"https://junk.io/": junk}))
    assert prof.ok


def test_findings_are_catalog_codes_with_http_evidence(acme):
    prof, _ = acme
    codes = {c.code for c in CATALOG}
    for f in prof.findings:
        assert f.code in codes
        assert f.evidence_url.startswith(("http://", "https://"))
        assert 0 <= f.confidence <= 1
