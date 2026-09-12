"""Small URL normalization helpers shared by request and planning models."""

_TRAILING_URL_PUNCTUATION = ".,;:!?\u060c\u061b\u061f]}"


def normalize_source_url(value: object) -> object:
    """Remove punctuation commonly attached to a URL in prose.

    This is deliberately only syntactic cleanup. Network and SSRF validation
    belongs to the safe extraction milestone.
    """

    if value is None:
        return value
    normalized = str(value).strip()
    while normalized and normalized[-1] in _TRAILING_URL_PUNCTUATION:
        normalized = normalized[:-1].rstrip()
    while normalized.endswith(")") and normalized.count(")") > normalized.count("("):
        normalized = normalized[:-1].rstrip()
    return normalized
