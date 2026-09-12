"""Safe URL fetching and bounded source extraction (Milestone 4)."""

from research_agent.errors import ExtractionError
from research_agent.extraction.contracts import (
    AddressValidator,
    DNSResolver,
    ExtractionConfig,
    FetchedPage,
    ReaderFallback,
)
from research_agent.extraction.fallback import JinaReaderFallback
from research_agent.extraction.fetcher import SafeFetcher
from research_agent.extraction.html import HTMLExtractor
from research_agent.extraction.security import (
    SafeURL,
    is_safe_ip,
    is_safe_url,
    validate_hostname,
    validate_ip_address,
    validate_resolved_addresses,
    validate_url,
)
from research_agent.extraction.service import SafeExtractor

__all__ = [
    "DNSResolver",
    "AddressValidator",
    "ExtractionConfig",
    "ExtractionError",
    "FetchedPage",
    "HTMLExtractor",
    "JinaReaderFallback",
    "ReaderFallback",
    "SafeExtractor",
    "SafeFetcher",
    "SafeURL",
    "is_safe_ip",
    "is_safe_url",
    "validate_hostname",
    "validate_ip_address",
    "validate_resolved_addresses",
    "validate_url",
]
