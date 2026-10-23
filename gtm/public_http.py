"""SSRF-safe HTTP for prospect websites (plan §6.2; adapted from Harvey's public_http.py, MIT).

A prospect's site is untrusted: a hostname can point at 127.0.0.1, at the cloud metadata service,
or redirect there. So for every request *and every redirect hop* we resolve DNS ourselves, refuse
if ANY answer is not a public address, then connect to the checked numeric IP while keeping the
original Host header and TLS SNI / certificate check for the hostname. Checking DNS and then
letting the HTTP library resolve again would leave a DNS-rebinding window.

Our additions to Harvey's client: a streamed response-size cap, connect/read timeouts plus a total
deadline, a polite User-Agent, and a per-domain minimum interval. Proxy env vars are ignored.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

DEFAULT_UA = "Mozilla/5.0 (compatible; gtm-tools/0.1; one-off B2B research fetch)"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}

# NAT64 prefixes embed an IPv4 address the IPv6 checks can't see.
_TRANSLATION_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

Resolver = Callable[[str, int], list[str]]


class PublicHTTPError(Exception):
    pass


class BlockedURL(PublicHTTPError):
    """Scheme, credentials, a non-public address, or too many redirects."""


class TooLarge(PublicHTTPError):
    """The response body exceeded max_bytes; the download was aborted."""


@dataclass
class Page:
    url: str  # final URL after redirects (with the hostname, never the pinned IP)
    status: int
    content_type: str
    text: str
    headers: dict[str, str]


def is_public_ip(ip: str) -> bool:
    """True only for globally routable unicast addresses with no embedded IPv4 trick."""
    if "%" in ip:  # scoped (zone-id) addresses are link-local by definition
        return False
    try:
        addr = ipaddress.ip_address(ip.strip("[]"))
    except ValueError:
        return False
    # is_global alone is not enough: it is True for multicast and for ::ffff:<public v4>.
    if (not addr.is_global or addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        return addr not in _CGNAT
    return not (addr.is_site_local or addr.ipv4_mapped or addr.sixtofour or addr.teredo
                or any(addr in net for net in _TRANSLATION_NETWORKS))


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class PublicClient:
    def __init__(self, *, user_agent: str = DEFAULT_UA, max_bytes: int = 2_000_000,
                 connect_timeout: float = 5, read_timeout: float = 15,
                 min_interval_per_domain: float = 1.0, max_redirects: int = 5,
                 resolver: Resolver | None = None, transport: httpx.BaseTransport | None = None):
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.min_interval = min_interval_per_domain
        self.resolver = resolver or system_resolver
        # A whole fetch may not take longer than this, so a server dripping one byte per
        # read_timeout can't hold a run hostage.
        self.total_timeout = connect_timeout + 2 * read_timeout
        # No keep-alive: different domains can share an IP, and a TLS connection verified for
        # one hostname must never carry another hostname's request.
        transport = transport or httpx.HTTPTransport(
            limits=httpx.Limits(max_keepalive_connections=0), retries=0)
        self._client = httpx.Client(
            transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout,
                                  write=read_timeout, pool=connect_timeout),
        )
        self._last_hit: dict[str, float] = {}
        self._now = time.monotonic
        self._sleep = time.sleep

    def __enter__(self) -> PublicClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, url: str) -> Page:
        """GET a public page. Non-2xx statuses are returned, not raised."""
        current = _parse(url)
        started = self._now()
        for _hop in range(self.max_redirects + 1):
            resp = self._fetch_once(current)
            location = resp.headers.get("location")
            if resp.status_code in REDIRECT_STATUSES and location:
                resp.close()
                current = _parse(str(current.join(location)))
                continue
            try:
                body = self._read_capped(resp, started)
            finally:
                resp.close()
            return Page(
                url=str(current),
                status=resp.status_code,
                content_type=resp.headers.get("content-type", ""),
                text=body.decode(resp.encoding or "utf-8", errors="replace"),
                headers={k.lower(): v for k, v in resp.headers.items()},
            )
        raise BlockedURL(f"more than {self.max_redirects} redirects starting at {url}")

    def _fetch_once(self, url: httpx.URL) -> httpx.Response:
        host = url.raw_host.decode("ascii")  # punycode: what DNS and TLS SNI actually use
        port = url.port or (443 if url.scheme == "https" else 80)
        addresses = self._checked_addresses(host, port)
        self._wait_turn(host)
        headers = {"Host": url.netloc.decode("ascii"), "User-Agent": self.user_agent,
                   "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"}
        for i, address in enumerate(addresses):
            request = self._client.build_request(
                "GET", url.copy_with(host=address), headers=headers,
                extensions={"sni_hostname": host},  # TLS SNI + cert check stay on the hostname
            )
            try:
                return self._client.send(request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # Nothing was sent yet, so trying the next checked address is safe.
                if i == len(addresses) - 1:
                    raise PublicHTTPError(f"could not connect to {host}: {e}") from e
            except httpx.HTTPError as e:
                raise PublicHTTPError(f"request to {host} failed: {e}") from e
        raise PublicHTTPError(f"no address to connect to for {host}")

    def _checked_addresses(self, host: str, port: int) -> list[str]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            answers = [str(literal)]
        else:
            try:
                answers = list(dict.fromkeys(self.resolver(host, port)))
            except OSError as e:
                raise PublicHTTPError(f"DNS lookup failed for {host}: {e}") from e
        if not answers:
            raise PublicHTTPError(f"DNS returned no addresses for {host}")
        bad = [a for a in answers if not is_public_ip(a)]
        if bad:
            raise BlockedURL(f"{host} resolves to a non-public address ({bad[0]})")
        # Prefer IPv4: many machines have no IPv6 route even when AAAA records exist.
        return sorted(answers, key=lambda a: ipaddress.ip_address(a).version)

    def _wait_turn(self, host: str) -> None:
        key = host.lower().rstrip(".")
        last = self._last_hit.get(key)
        if last is not None:
            wait = self.min_interval - (self._now() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[key] = self._now()

    def _read_capped(self, resp: httpx.Response, started: float) -> bytes:
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            raise TooLarge(f"{declared} bytes declared, limit {self.max_bytes}")
        chunks: list[bytes] = []
        size = 0
        try:
            # iter_bytes decompresses, so the cap also stops gzip bombs.
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > self.max_bytes:
                    raise TooLarge(f"body over {self.max_bytes} bytes; aborted")
                if self._now() - started > self.total_timeout:
                    raise PublicHTTPError(f"fetch took over {self.total_timeout:.0f}s; aborted")
                chunks.append(chunk)
        except httpx.HTTPError as e:
            raise PublicHTTPError(f"reading the response failed: {e}") from e
        return b"".join(chunks)


def _parse(url: str) -> httpx.URL:
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as e:
        raise BlockedURL(f"not a valid URL: {url!r}") from e
    if parsed.scheme not in ("http", "https"):
        raise BlockedURL(f"only http/https allowed, got {parsed.scheme or 'no'} scheme")
    if parsed.userinfo:
        raise BlockedURL("credentials in URLs are not allowed")
    host = parsed.host.rstrip(".").lower()
    if not host:
        raise BlockedURL(f"no host in {url!r}")
    if host == "localhost" or host.endswith(".localhost"):
        raise BlockedURL("localhost is not a public website")
    return parsed
