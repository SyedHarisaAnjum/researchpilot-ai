"""SSRF protection: only allow URLs that resolve to public internet addresses."""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443, 8080, 8443}
MAX_URL_LENGTH = 2048

BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")


class UnsafeURLError(ValueError):
    pass


class HostResolutionError(Exception):
    """DNS lookup failed. Not a safety problem, but the page can't be fetched."""


@dataclass(frozen=True)
class SafeTarget:
    """A validated URL plus the IP address it must be fetched from.

    Connecting to the IP we checked (instead of resolving the hostname again)
    stops DNS rebinding, where a host resolves to a public IP during the
    check and to a private one when the request is made.
    """

    url: str
    scheme: str
    host: str
    port: int
    ip: str

    @property
    def pinned_url(self) -> str:
        parts = urlsplit(self.url)
        ip_host = f"[{self.ip}]" if ":" in self.ip else self.ip
        return urlunsplit((self.scheme, f"{ip_host}:{self.port}", parts.path or "/", parts.query, ""))

    @property
    def host_header(self) -> str:
        default_port = 443 if self.scheme == "https" else 80
        return self.host if self.port == default_port else f"{self.host}:{self.port}"


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv6 forms that embed an IPv4 address are judged by that address.
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is not None:
            return is_public_ip(embedded)
    # is_global excludes loopback, private, link-local (169.254.x.x, incl. cloud
    # metadata), carrier-grade NAT, reserved and documentation ranges.
    return ip.is_global and not ip.is_multicast


def validate_url(url: str) -> tuple[str, str, int]:
    """Check a URL's syntax and return (scheme, hostname, port). No network access."""
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError("URL is too long")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError(f"Invalid URL: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Scheme '{scheme or 'none'}' is not allowed")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with credentials are not allowed")
    if not parts.hostname:
        raise UnsafeURLError("URL has no hostname")

    host = parts.hostname.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeURLError("Invalid hostname") from exc
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURLError(f"Host '{host}' is not allowed")

    port = port or (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError(f"Port {port} is not allowed")

    try:
        if not is_public_ip(ipaddress.ip_address(host)):
            raise UnsafeURLError(f"Address {host} is not a public IP")
    except ValueError:
        pass  # Not an IP literal; checked after DNS resolution.
    return scheme, host, port


async def resolve_safe_target(url: str) -> SafeTarget:
    """Validate a URL and resolve it. Every resolved address must be public."""
    scheme, host, port = validate_url(url)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HostResolutionError(f"Could not resolve host '{host}'") from exc

    addresses = {info[4][0].split("%")[0] for info in infos}
    if not addresses:
        raise HostResolutionError(f"Could not resolve host '{host}'")
    for address in addresses:
        if not is_public_ip(ipaddress.ip_address(address)):
            raise UnsafeURLError(f"Host '{host}' resolves to a non-public address")

    # Prefer IPv4: it works on more networks.
    ip = sorted(addresses, key=lambda a: ":" in a)[0]
    return SafeTarget(url=url.strip(), scheme=scheme, host=host, port=port, ip=ip)
