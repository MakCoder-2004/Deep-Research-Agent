"""URL, IP, and DNS-answer validation for the safe extraction boundary."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

from research_agent.errors import ErrorCategory, ExtractionError

__all__ = [
    "SafeURL",
    "is_safe_ip",
    "is_safe_url",
    "validate_hostname",
    "validate_ip_address",
    "validate_resolved_addresses",
    "validate_url",
]

_METADATA_HOSTNAMES = frozenset(
    {
        "instance-data",
        "metadata",
        "metadata.google.internal",
        "metadata.azure.internal",
    }
)
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


@dataclass(frozen=True, slots=True)
class SafeURL:
    """Normalized URL plus the exact hostname and port to pin for a request."""

    url: str
    scheme: str
    hostname: str
    port: int

    @property
    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.hostname, self.port

    def __str__(self) -> str:
        return self.url


def _invalid(message: str) -> ExtractionError:
    return ExtractionError(message, category=ErrorCategory.INVALID_REQUEST)


def _ssrf(message: str) -> ExtractionError:
    return ExtractionError(message, category=ErrorCategory.SSRF)


def _dns(message: str) -> ExtractionError:
    return ExtractionError(message, category=ErrorCategory.DNS)


def _literal_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Recognize normal and common legacy numeric IPv4 spellings."""
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass
    if not hostname:
        return None
    # URL clients and operating systems have historically accepted integer,
    # hexadecimal, and abbreviated IPv4 forms.  Treat them as IP literals so
    # they cannot evade the address policy by being sent to a resolver.
    if hostname.isdigit():
        try:
            value = int(hostname, 10)
            if 0 <= value <= 2**32 - 1:
                return ipaddress.ip_address(value)
        except ValueError:
            return None
    if hostname.lower().startswith("0x"):
        try:
            value = int(hostname, 16)
            if 0 <= value <= 2**32 - 1:
                return ipaddress.ip_address(value)
        except ValueError:
            return None
    if "." in hostname:
        try:
            return ipaddress.ip_address(socket.inet_aton(hostname))
        except (OSError, ValueError):
            return None
    return None


def validate_ip_address(address: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Validate one resolved address against the complete SSRF deny policy."""
    text = str(address).strip()
    if not text or "%" in text:
        raise _dns("DNS returned an empty or scoped address.")
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError as exc:
        raise _dns("DNS returned a non-IP address.") from exc
    if parsed in _METADATA_IPS:
        raise _ssrf("The destination resolves to a metadata service address.")
    mapped = parsed.ipv4_mapped if isinstance(parsed, ipaddress.IPv6Address) else None
    policy_address = mapped or parsed
    if (
        policy_address.is_loopback
        or policy_address.is_private
        or policy_address.is_link_local
        or policy_address.is_multicast
        or policy_address.is_unspecified
        or policy_address.is_reserved
        or not policy_address.is_global
    ):
        raise _ssrf("The destination resolves to a non-public network address.")
    return parsed


def is_safe_ip(address: object) -> bool:
    """Return whether an address is publicly routable under the extraction policy."""
    try:
        validate_ip_address(address)
    except ExtractionError:
        return False
    return True


def validate_resolved_addresses(addresses: list[object] | tuple[object, ...]) -> tuple[str, ...]:
    """Validate every DNS answer, rejecting mixed public/private responses."""
    if not addresses:
        raise _dns("Hostname resolution returned no addresses.")
    normalized: list[str] = []
    for address in addresses:
        parsed = validate_ip_address(address)
        value = str(parsed)
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def validate_hostname(hostname: str) -> str:
    """Normalize a hostname and reject obvious local or metadata names."""
    host = hostname.strip().lower().rstrip(".")
    if not host:
        raise _invalid("URL must contain a hostname.")
    if host in _METADATA_HOSTNAMES or host == "localhost" or host.endswith(".localhost"):
        raise _ssrf("Local and metadata hostnames are not allowed.")
    literal = _literal_ip(host)
    if literal is not None:
        validate_ip_address(literal)
        return str(literal)
    if len(host) > 253:
        raise _invalid("URL hostname is too long.")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise _invalid("URL hostname is not valid IDNA syntax.") from exc
    labels = ascii_host.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(char.isalnum() or char == "-" for char in label)
        for label in labels
    ):
        raise _invalid("URL hostname is not valid DNS syntax.")
    # Numeric-only hostnames are accepted by some client stacks as alternate
    # IPv4 notation.  A multi-label numeric name is already handled above.
    if len(labels) == 1 and labels[0].isdigit():
        raise _invalid("Numeric hostname is not valid public DNS syntax.")
    return ascii_host


def _parts_to_safe_url(parts: SplitResult, scheme: str, hostname: str, port: int) -> SafeURL:
    host_for_url = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_for_url if port == (80 if scheme == "http" else 443) else f"{host_for_url}:{port}"
    normalized = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
    return SafeURL(normalized, scheme, hostname, port)


def normalize_url(url: object) -> SafeURL:
    """Parse and validate a URL before any DNS or HTTP operation."""
    if isinstance(url, bytes):
        raise _invalid("URL must be text, not bytes.")
    raw = str(url).strip()
    if not raw or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw):
        raise _invalid("URL is empty or contains whitespace/control characters.")
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            raise _invalid("Only HTTP and HTTPS URLs are allowed.")
        if not parts.netloc or "@" in parts.netloc or parts.username is not None:
            raise _invalid("URL credentials are not allowed.")
        hostname = parts.hostname
        if hostname is None:
            raise _invalid("URL must contain a hostname.")
        try:
            port = parts.port
        except ValueError as exc:
            raise _invalid("URL port is invalid.") from exc
        if port is None:
            port = 80 if scheme == "http" else 443
        if port < 1 or port > 65535:
            raise _invalid("URL port must be between 1 and 65535.")
        normalized_host = validate_hostname(hostname)
        return _parts_to_safe_url(parts, scheme, normalized_host, port)
    except ExtractionError:
        raise
    except ValueError as exc:
        raise _invalid("URL syntax is invalid.") from exc


def validate_url(url: object) -> str:
    """Return a normalized safe URL or raise :class:`ExtractionError`."""
    return normalize_url(url).url


def is_safe_url(url: object) -> bool:
    """Return whether URL syntax and literal-host policy validation succeed."""
    try:
        normalize_url(url)
    except ExtractionError:
        return False
    return True
