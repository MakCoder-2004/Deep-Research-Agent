"""Deterministic HTML cleanup and metadata extraction.

This module only parses bytes.  It never evaluates scripts, follows links, or
interprets text as instructions.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, Tag
from pydantic import HttpUrl, TypeAdapter

from research_agent.errors import ErrorCategory, ExtractionError
from research_agent.extraction.contracts import ExtractionConfig, FetchedPage
from research_agent.extraction.security import validate_url
from research_agent.models.research import SourceDocument

_HTTP_URL = TypeAdapter(HttpUrl)
_WHITESPACE_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b")
_REMOVE_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "iframe",
        "object",
        "embed",
        "form",
        "nav",
        "menu",
        "aside",
        "footer",
    }
)
_BOILERPLATE_MARKERS = frozenset(
    {
        "ad",
        "ads",
        "advert",
        "advertisement",
        "banner",
        "breadcrumb",
        "cookie",
        "consent",
        "footer",
        "header",
        "login",
        "menu",
        "modal",
        "nav",
        "newsletter",
        "pagination",
        "paywall",
        "popup",
        "promo",
        "recommend",
        "related",
        "share",
        "sidebar",
        "social",
        "subscribe",
        "toolbar",
    }
)
_TEXT_BOILERPLATE_RE = re.compile(
    r"\b(?:advert(?:isement)?|cookie(?:s| notice)?|newsletter|privacy settings|"
    r"related articles|sign[ -]?up|subscribe)\b",
    re.IGNORECASE,
)


class HTMLExtractor:
    """Create one bounded :class:`SourceDocument` from a fetched HTML page."""

    def __init__(self, config: ExtractionConfig | None = None) -> None:
        self.config = config or ExtractionConfig()

    def extract(
        self,
        page: FetchedPage,
        *,
        source_id: int,
        source_title: str = "",
        source_publisher: str | None = None,
        source_published_at: datetime | None = None,
        source_url: str | None = None,
    ) -> SourceDocument:
        try:
            soup = BeautifulSoup(page.content, "lxml")
        except Exception as exc:
            raise ExtractionError(
                "The HTML document could not be parsed.",
                category=ErrorCategory.EXTRACTION,
                url=page.final_url,
            ) from exc

        title = _first_value(
            _meta_values(soup, names=("title",), properties=("og:title", "twitter:title"))
            + [_tag_text(soup.find("title")), _tag_text(soup.find("h1")), source_title]
        )
        author = _first_value(
            _meta_values(soup, names=("author", "byline"), properties=("article:author",))
            + [_tag_text(_author_tag(soup))]
        )
        publisher = _first_value(
            _meta_values(
                soup,
                names=("publisher", "source", "application-name"),
                properties=("og:site_name",),
            )
            + [source_publisher]
        )
        published_at = (
            _parse_date(
                _first_value(
                    _meta_values(
                        soup,
                        names=("date", "pubdate", "publishdate", "datepublished"),
                        properties=("article:published_time", "og:published_time"),
                    )
                    + [_tag_attr(soup.find("time"), "datetime")]
                )
            )
            or source_published_at
        )
        json_ld = _json_ld_metadata(soup)
        title = title or _text_value(json_ld.get("headline")) or _text_value(json_ld.get("name"))
        author = author or _named_value(json_ld.get("author"))
        publisher = publisher or _named_value(json_ld.get("publisher"))
        published_at = published_at or _parse_date(_text_value(json_ld.get("datePublished")))

        _remove_untrusted_or_boilerplate(soup)
        root = _content_root(soup)
        lines = _text_lines(root)
        body_text = _bounded_text("\n".join(lines), self.config.max_source_chars)
        if not body_text:
            raise ExtractionError(
                "The HTML document contained no extractable text.",
                category=ErrorCategory.EXTRACTION,
                url=page.final_url,
            )
        headings = _unique_bounded(
            [
                _clean_text(_tag_text(tag))
                for tag in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
            ],
            500,
        )
        links = _extract_links(root, page.final_url, self.config.max_links)
        quotations = _extract_quotations(
            root,
            lines,
            max_quotations=self.config.max_quotations,
            max_chars=self.config.quotation_max_chars,
        )
        return SourceDocument(
            source_id=source_id,
            url=_as_http_url(source_url or page.final_url),
            requested_url=_as_http_url(source_url or page.requested_url),
            title=_bounded_text(_clean_text(title or ""), 500),
            author=_optional_bounded(author, 300),
            publisher=_optional_bounded(publisher, 300),
            published_at=published_at,
            headings=headings,
            body_text=body_text,
            links=links,
            quotations=quotations,
            fetch_ms=page.fetch_ms,
            extraction_tool="beautifulsoup",
            fetch_requested_url=_as_http_url(page.requested_url),
            fetch_final_url=_as_http_url(page.final_url),
            status_code=page.status_code,
            content_type=page.content_type,
            bytes_read=page.bytes_read,
        )


def _remove_untrusted_or_boilerplate(soup: BeautifulSoup) -> None:
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for element in list(soup.find_all(True)):
        if not isinstance(element, Tag):
            continue
        if element.attrs is None:
            continue
        tag_name = element.name.lower() if element.name else ""
        classes = str(element.get("class") or "")
        identity_values = [
            classes,
            str(element.get("id", "")),
            str(element.get("aria-label", "")),
            str(element.get("data-testid", "")),
            str(element.get("data-component", "")),
            str(element.get("data-purpose", "")),
        ]
        identity = " ".join(identity_values).casefold()
        marker_tokens = set(re.findall(r"[a-z0-9]+", identity))
        role = str(element.get("role", "")).casefold()
        aria_hidden = str(element.get("aria-hidden", "")).casefold()
        is_content_header = tag_name == "header" and _inside_content_container(element)
        matched_markers = marker_tokens & _BOILERPLATE_MARKERS
        is_marked_article_header = _inside_content_container(element) and matched_markers == {
            "header"
        }
        is_text_boilerplate = (
            tag_name in {"div", "section"}
            and not element.find("article")
            and len(_clean_text(element.get_text(" ", strip=True))) <= 800
            and _TEXT_BOILERPLATE_RE.search(element.get_text(" ", strip=True)) is not None
            and (not element.find_all("p") or element.find(["form", "button", "input"]) is not None)
        )
        if (
            (tag_name in _REMOVE_TAGS and not is_content_header)
            or (tag_name == "header" and not is_content_header)
            or role in {"navigation", "banner", "complementary", "contentinfo"}
            or aria_hidden == "true"
            or (bool(matched_markers) and not is_marked_article_header)
            or is_text_boilerplate
        ):
            element.decompose()


def _inside_content_container(element: Tag) -> bool:
    for parent in element.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name in {"article", "main"}:
            return True
        if str(parent.get("role", "")).casefold() == "main":
            return True
    return False


def _content_root(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    candidates = [tag for tag in soup.find_all(["main", "article"]) if isinstance(tag, Tag)]
    role_main = next(
        (tag for tag in soup.find_all(True) if str(tag.get("role", "")).casefold() == "main"),
        None,
    )
    if isinstance(role_main, Tag):
        candidates.append(role_main)
    if candidates:
        return max(candidates, key=lambda tag: len(_clean_text(tag.get_text(" ", strip=True))))
    body = soup.find("body")
    return body if isinstance(body, Tag) else soup


def _text_lines(root: Tag | BeautifulSoup) -> list[str]:
    raw = root.get_text("\n", strip=True)
    candidates = [_clean_text(line) for line in raw.splitlines()]
    candidates = [line for line in candidates if line]
    counts = Counter(candidates)
    lines: list[str] = []
    for line in candidates:
        # Repeated short lines are usually menus, legal labels, or related-
        # article boilerplate.  Keep repeated long paragraphs as evidence.
        if counts[line] > 1 and len(line) <= 200:
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return lines


def _extract_links(root: Tag | BeautifulSoup, base_url: str, maximum: int) -> list[HttpUrl]:
    links: list[HttpUrl] = []
    seen: set[str] = set()
    for anchor in root.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith("#"):
            continue
        try:
            candidate = validate_url(urljoin(base_url, href))
            if candidate in seen:
                continue
            seen.add(candidate)
            links.append(_as_http_url(candidate))
        except ExtractionError:
            # Links are metadata, not fetch targets.  Skip malformed and
            # private links without failing an otherwise useful document.
            continue
        if len(links) >= maximum:
            break
    return links


def _extract_quotations(
    root: Tag | BeautifulSoup,
    body_lines: list[str],
    *,
    max_quotations: int,
    max_chars: int,
) -> list[str]:
    body_set = set(body_lines)
    quotations: list[str] = []
    for element in root.find_all(["p", "blockquote", "li"]):
        text = _clean_text(element.get_text(" ", strip=True))
        if not text or text not in body_set:
            continue
        quote = _bounded_text(text, max_chars)
        if quote not in quotations:
            quotations.append(quote)
        if len(quotations) >= max_quotations:
            break
    if not quotations and body_lines:
        quotations = [_bounded_text(body_lines[0], max_chars)]
    return quotations[:max_quotations]


def _meta_values(
    soup: BeautifulSoup,
    *,
    names: tuple[str, ...],
    properties: tuple[str, ...],
) -> list[str]:
    values: list[str] = []
    wanted_names = {value.casefold() for value in names}
    wanted_properties = {value.casefold() for value in properties}
    for tag in soup.find_all("meta"):
        name = str(tag.get("name", "")).casefold()
        prop = str(tag.get("property", "")).casefold()
        if name in wanted_names or prop in wanted_properties:
            content = _clean_text(str(tag.get("content", "")))
            if content:
                values.append(content)
    return values


def _json_ld_metadata(soup: BeautifulSoup) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            value = json.loads(script.string or script.get_text())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for item in _json_objects(value):
            for key in ("headline", "name", "author", "publisher", "datePublished"):
                if key not in output and key in item:
                    output[key] = item[key]
    return output


def _json_objects(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        output = [value]
        for nested in value.values():
            output.extend(_json_objects(nested))
        return output
    if isinstance(value, list):
        objects: list[dict[str, Any]] = []
        for nested in value:
            objects.extend(_json_objects(nested))
        return objects
    return []


def _named_value(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text_value(value.get("name"))
    if isinstance(value, list):
        for item in value:
            result = _named_value(item)
            if result:
                return result
    return _text_value(value)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            match = _DATE_RE.search(text)
            if not match:
                return None
            try:
                parsed = datetime.fromisoformat(match.group(0))
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _first_value(values: Sequence[str | None]) -> str | None:
    for value in values:
        text = _clean_text(value or "")
        if text:
            return text
    return None


def _tag_text(tag: object) -> str:
    return tag.get_text(" ", strip=True) if isinstance(tag, Tag) else ""


def _author_tag(soup: BeautifulSoup) -> Tag | None:
    for tag in soup.find_all("a"):
        rel = tag.get("rel")
        if isinstance(rel, (list, tuple)) and "author" in rel:
            return tag
        if isinstance(rel, str) and rel.casefold() == "author":
            return tag
    return None


def _tag_attr(tag: object, name: str) -> str | None:
    if not isinstance(tag, Tag):
        return None
    value = tag.get(name)
    return str(value) if value is not None else None


def _text_value(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _clean_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def _bounded_text(value: str, maximum: int) -> str:
    text = value[:maximum]
    if len(value) > maximum and " " in text:
        text = text.rsplit(" ", 1)[0]
    return text.rstrip()


def _optional_bounded(value: str | None, maximum: int) -> str | None:
    return _bounded_text(_clean_text(value), maximum) if value and _clean_text(value) else None


def _unique_bounded(values: list[str], maximum: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(_bounded_text(value, maximum))
    return output


def _as_http_url(value: str) -> HttpUrl:
    return _HTTP_URL.validate_python(value)
