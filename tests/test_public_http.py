"""SSRF-safe client: DNS and transport are mocked, so nothing here touches the network."""

import httpx
import pytest

from gtm.public_http import BlockedURL, Page, PublicClient, PublicHTTPError, TooLarge, is_public_ip

PUBLIC = "93.184.216.34"


class Recorder:
    """A MockTransport handler that records every request and answers from a route table."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.headers["host"], request.url.path)
        status, headers, body = self.routes.get(key, (200, {"content-type": "text/html"}, b"<p>ok</p>"))
        return httpx.Response(status, headers=headers, content=body)


def client(dns, handler=None, **kw):
    """dns: {host: [ips]} or a callable(host, port)."""
    resolver = dns if callable(dns) else (lambda host, port: dns[host])
    handler = handler or Recorder()
    kw.setdefault("min_interval_per_domain", 0)
    return PublicClient(resolver=resolver, transport=httpx.MockTransport(handler), **kw), handler


@pytest.mark.parametrize("ip", [
    "10.0.0.5", "172.16.1.1", "192.168.1.10", "127.0.0.1", "169.254.169.254", "100.64.0.1",
    "0.0.0.0", "224.0.0.1", "240.0.0.1", "::1", "fe80::1", "fd00::1", "::", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:8.8.8.8", "64:ff9b::808:808", "2002:c000:0204::1", "fe80::1%eth0",
    "not-an-ip",
])
def test_non_public_addresses_rejected(ip):
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip):
    assert is_public_ip(ip)


def test_happy_path_connects_to_checked_ip_with_host_and_sni():
    c, rec = client({"example.com": [PUBLIC]})
    page = c.get("https://example.com/about")
    assert isinstance(page, Page)
    assert (page.url, page.status, page.text) == ("https://example.com/about", 200, "<p>ok</p>")
    assert page.content_type == "text/html"
    req = rec.requests[0]
    assert req.url.host == PUBLIC                      # pinned to the address we checked
    assert req.headers["host"] == "example.com"        # virtual host kept
    assert req.extensions["sni_hostname"] == "example.com"  # TLS SNI + cert check on the name
    assert "gtm-tools" in req.headers["user-agent"]


def test_idn_host_uses_punycode_for_dns_and_sni():
    seen = []
    c, rec = client(lambda host, port: seen.append(host) or [PUBLIC])
    c.get("https://bücher.example/")
    assert seen == ["xn--bcher-kva.example"]
    assert rec.requests[0].extensions["sni_hostname"] == "xn--bcher-kva.example"


def test_private_ip_literal_blocked():
    c, rec = client({})
    with pytest.raises(BlockedURL):
        c.get("http://10.0.0.5/admin")
    assert rec.requests == []


def test_loopback_via_dns_blocked():
    c, rec = client({"evil.example": ["127.0.0.1"]})
    with pytest.raises(BlockedURL):
        c.get("http://evil.example/")
    assert rec.requests == []


def test_any_private_answer_blocks_even_with_a_public_one():
    c, rec = client({"mixed.example": [PUBLIC, "10.1.2.3"]})
    with pytest.raises(BlockedURL):
        c.get("https://mixed.example/")
    assert rec.requests == []


def test_ipv4_mapped_ipv6_blocked():
    c, rec = client({"mapped.example": ["::ffff:127.0.0.1"]})
    with pytest.raises(BlockedURL):
        c.get("https://mapped.example/")
    with pytest.raises(BlockedURL):
        c.get("http://[::ffff:169.254.169.254]/latest/meta-data/")
    assert rec.requests == []


def test_redirect_to_metadata_ip_blocked():
    rec = Recorder({("example.com", "/"): (302, {"location": "http://169.254.169.254/latest/meta-data/"}, b"")})
    c, _ = client({"example.com": [PUBLIC]}, rec)
    with pytest.raises(BlockedURL):
        c.get("https://example.com/")
    assert len(rec.requests) == 1  # the metadata address was never contacted


def test_redirect_to_host_resolving_private_blocked():
    rec = Recorder({("example.com", "/"): (301, {"location": "https://internal.example/x"}, b"")})
    c, _ = client({"example.com": [PUBLIC], "internal.example": ["192.168.0.10"]}, rec)
    with pytest.raises(BlockedURL):
        c.get("https://example.com/")
    assert len(rec.requests) == 1


def test_dns_rebinding_second_answer_private_is_blocked():
    answers = iter([[PUBLIC], ["127.0.0.1"]])
    rec = Recorder({("rebind.example", "/"): (302, {"location": "/next"}, b"")})
    c, _ = client(lambda host, port: next(answers), rec)
    with pytest.raises(BlockedURL):
        c.get("http://rebind.example/")
    # The one request that went out was pinned to the public answer, never re-resolved.
    assert [r.url.host for r in rec.requests] == [PUBLIC]


def test_relative_redirect_followed_and_final_url_reported():
    rec = Recorder({("example.com", "/old"): (308, {"location": "/new"}, b"")})
    c, _ = client({"example.com": [PUBLIC]}, rec)
    page = c.get("https://example.com/old")
    assert page.url == "https://example.com/new"
    assert [r.url.path for r in rec.requests] == ["/old", "/new"]


def test_too_many_redirects():
    rec = Recorder({("loop.example", "/"): (302, {"location": "/"}, b"")})
    c, _ = client({"loop.example": [PUBLIC]}, rec, max_redirects=5)
    with pytest.raises(BlockedURL, match="redirects"):
        c.get("http://loop.example/")
    assert len(rec.requests) == 6  # the original + 5 hops


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "javascript:alert(1)",
    "https://user:pass@example.com/", "http://localhost:8080/", "http://app.localhost/",
])
def test_bad_schemes_credentials_and_localhost_blocked(url):
    c, rec = client({"example.com": [PUBLIC]})
    with pytest.raises(BlockedURL):
        c.get(url)
    assert rec.requests == []


def test_redirect_to_file_scheme_blocked():
    rec = Recorder({("example.com", "/"): (302, {"location": "file:///etc/passwd"}, b"")})
    c, _ = client({"example.com": [PUBLIC]}, rec)
    with pytest.raises(BlockedURL):
        c.get("https://example.com/")


def test_oversize_body_aborted():
    big = b"x" * 5000
    rec = Recorder({("example.com", "/big"): (200, {"content-type": "text/html"}, big)})
    c, _ = client({"example.com": [PUBLIC]}, rec, max_bytes=1000)
    with pytest.raises(TooLarge):
        c.get("https://example.com/big")


def test_oversize_declared_length_aborted_before_reading():
    def handler(request):
        def never_read():
            raise AssertionError("body should not be read")
            yield b""
        return httpx.Response(200, headers={"content-length": "999999999"}, content=never_read())
    c, _ = client({"example.com": [PUBLIC]}, handler, max_bytes=1000)
    with pytest.raises(TooLarge):
        c.get("https://example.com/")


def test_non_2xx_returned_not_raised():
    rec = Recorder({("example.com", "/missing"): (404, {"content-type": "text/plain"}, b"nope")})
    c, _ = client({"example.com": [PUBLIC]}, rec)
    page = c.get("https://example.com/missing")
    assert (page.status, page.text) == (404, "nope")


def test_charset_respected():
    rec = Recorder({("example.com", "/"): (200, {"content-type": "text/html; charset=latin-1"}, "café".encode("latin-1"))})
    c, _ = client({"example.com": [PUBLIC]}, rec)
    assert c.get("https://example.com/").text == "café"


def test_dns_failure_is_a_public_http_error():
    def resolver(host, port):
        raise OSError("NXDOMAIN")
    c, _ = client(resolver)
    with pytest.raises(PublicHTTPError):
        c.get("https://nope.example/")


def test_connect_error_tries_next_checked_address():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "1.1.1.1":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, content=b"ok")
    c, _ = client({"multi.example": ["1.1.1.1", "8.8.8.8"]}, handler)
    assert c.get("https://multi.example/").text == "ok"
    assert seen == ["1.1.1.1", "8.8.8.8"]


def test_read_timeout_is_a_public_http_error():
    def handler(request):
        raise httpx.ReadTimeout("slow")
    c, _ = client({"example.com": [PUBLIC]}, handler)
    with pytest.raises(PublicHTTPError):
        c.get("https://example.com/")


def test_proxy_env_ignored(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.1:3128")
    c = PublicClient(resolver=lambda h, p: [PUBLIC])
    assert c._client._mounts == {}  # no proxy transports mounted from the environment
    c.close()


def test_per_domain_rate_limit_waits():
    c, _ = client({"example.com": [PUBLIC], "other.example": [PUBLIC]}, min_interval_per_domain=2.0)
    clock = [100.0]
    sleeps = []
    c._now = lambda: clock[0]
    c._sleep = lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s))
    c.get("https://example.com/a")
    c.get("https://other.example/a")  # a different domain doesn't wait
    clock[0] += 0.5
    c.get("https://example.com/b")
    assert sleeps == [pytest.approx(1.5)]


def test_context_manager_closes():
    with client({"example.com": [PUBLIC]})[0] as c:
        c.get("https://example.com/")
    assert c._client.is_closed
