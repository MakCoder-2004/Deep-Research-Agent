"""Deterministic query analyzer (M3.1-M3.7).

Produces a validated :class:`ResearchPlan` without LLM or network calls.
All rules are deterministic so CI stays quota-free and tests are reproducible.

Covers PLAN section 9:

- M3.1: English / Arabic / mixed detection via the Arabic Unicode blocks.
- M3.2: domain, locality, jurisdiction, freshness, and risk classification.
- M3.3: quick / standard / deep depth rules with matching search budgets.
- M3.4: focused subquestions plus English/Arabic query variants when useful.
- M3.5: source-category, tool, source-budget, and time-budget selection.
- M3.6: clarification gate for materially ambiguous requests.
- M3.7: a validated structured ``ResearchPlan`` (never prose).
"""

from __future__ import annotations

import re

from research_agent.models import Depth, Domain, Language, RiskLevel
from research_agent.models.requests import ResearchRequest
from research_agent.models.research import ResearchPlan

__all__ = [
    "analyze_query",
    "analyze_request",
    "assess_risk",
    "build_query_variants",
    "build_subquestions",
    "classify_domain",
    "detect_language",
    "detect_locality",
    "is_ambiguous",
    "requires_freshness",
    "select_depth",
    "select_tools",
]

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WORD_RE = re.compile(r"[\w\u0600-\u06FF]+", re.UNICODE)

_VAGUE_QUERIES = frozenset(
    {
        "help",
        "hi",
        "hello",
        "test",
        "news",
        "research",
        "what",
        "what?",
        "why?",
        "how?",
        "?",
        "؟",
        "بحث",
        "ابحث",
        "أخبار",
        "اخبار",
        "موضوع",
        "استفسار",
        "سؤال",
        "tell me about it",
        "more info",
        "what about it",
        "explain this",
        "do research",
        "search this",
        "أخبرني",
        "أخبرني عنه",
        "ما رأيك",
    }
)

_MEDICAL_KEYWORDS = frozenset(
    {
        "medical",
        "medicine",
        "health",
        "disease",
        "diagnosis",
        "treatment",
        "dosage",
        "drug",
        "vaccine",
        "clinical",
        "symptom",
        "syndrome",
        "therapy",
        "pubmed",
        "patient",
        "doctor",
        "hospital",
        "epidemic",
        "pandemic",
        "صحة",
        "صحي",
        "طبية",
        "طبي",
        "مرض",
        "علاج",
        "دواء",
        "لقاح",
        "تشخيص",
        "أعراض",
        "اعراض",
        "مستشفى",
        "عيادة",
        "وباء",
    }
)

_LEGAL_KEYWORDS = frozenset(
    {
        "law",
        "legal",
        "court",
        "regulation",
        "statute",
        "contract",
        "lawsuit",
        "lawyer",
        "legislation",
        "constitution",
        "compliance",
        "jurisdiction",
        "verdict",
        "قانون",
        "قانوني",
        "محكمة",
        "تشريع",
        "عقد",
        "قضاء",
        "دعوى",
        "محامي",
        "دستور",
        "لائحة",
        "نظام",
    }
)

_FINANCIAL_KEYWORDS = frozenset(
    {
        "finance",
        "financial",
        "investment",
        "investor",
        "stock",
        "market",
        "bank",
        "banking",
        "crypto",
        "bitcoin",
        "economy",
        "economic",
        "inflation",
        "budget",
        "tax",
        "forex",
        "trading",
        "interest rate",
        "gdp",
        "مال",
        "مالي",
        "بنك",
        "استثمار",
        "سوق",
        "أسهم",
        "اسهم",
        "اقتصاد",
        "اقتصادي",
        "تضخم",
        "ميزانية",
        "ضريبة",
        "تداول",
        "عملة",
    }
)

_ACADEMIC_KEYWORDS = frozenset(
    {
        "paper",
        "journal",
        "thesis",
        "dissertation",
        "peer-reviewed",
        "peer reviewed",
        "doi",
        "arxiv",
        "semantic scholar",
        "university",
        "professor",
        "study",
        "research",
        "literature review",
        "systematic review",
        "meta-analysis",
        "citation",
        "بحث",
        "دراسة",
        "جامعة",
        "أكاديمي",
        "اكاديمي",
        "رسالة",
        "أطروحة",
        "اطروحة",
        "مجلة علمية",
        "مراجعة منهجية",
    }
)

_TECHNICAL_KEYWORDS = frozenset(
    {
        "code",
        "coding",
        "github",
        "python",
        "api",
        "software",
        "programming",
        "kubernetes",
        "docker",
        "stackoverflow",
        "algorithm",
        "database",
        "framework",
        "sdk",
        "repository",
        "release",
        "issue",
        "pull request",
        "برمجة",
        "كود",
        "تقنية",
        "تقني",
        "خوارزمية",
        "برمجيات",
        "قاعدة بيانات",
        "مستودع",
        "واجهة برمجية",
    }
)

_NEWS_KEYWORDS = frozenset(
    {
        "news",
        "breaking",
        "headline",
        "election",
        "war",
        "conflict",
        "summit",
        "press conference",
        "gdelt",
        "أخبار",
        "اخبار",
        "عاجل",
        "انتخابات",
        "حرب",
        "قمة",
        "مؤتمر صحفي",
    }
)

_FRESHNESS_KEYWORDS = frozenset(
    {
        "latest",
        "current",
        "today",
        "now",
        "recent",
        "update",
        "breaking",
        "live",
        "2025",
        "2026",
        "this week",
        "this month",
        "price",
        "election",
        "news",
        "الأخير",
        "الاخير",
        "آخر",
        "اخر",
        "الحالي",
        "اليوم",
        "الآن",
        "الان",
        "عاجل",
        "جديد",
        "حديث",
        "مستجد",
        "أخبار",
        "اخبار",
    }
)

_CONTESTED_KEYWORDS = frozenset(
    {
        "contested",
        "controversial",
        "controversy",
        "debate",
        "disputed",
        "conflicting",
        "مثير للجدل",
        "جدل",
        "خلاف",
        "متنازع",
    }
)

_COMPARISON_KEYWORDS = frozenset(
    {
        "compare",
        "comparison",
        "versus",
        " vs ",
        "pros and cons",
        "differences",
        "explain",
        "how does",
        "why does",
        "overview",
        "guide",
        "analysis",
        "قارن",
        "مقارنة",
        "اشرح",
        "شرح",
        "لماذا",
        "كيف",
        "دليل",
        "تحليل",
        "الفرق بين",
    }
)

# Canonical MENA jurisdiction -> accepted aliases (lowercased, both scripts).
_MENA_ALIASES: dict[str, frozenset[str]] = {
    "Egypt": frozenset({"egypt", "egyptian", "cairo", "مصر", "مصري", "القاهرة"}),
    "Saudi Arabia": frozenset({"saudi", "saudi arabia", "riyadh", "السعودية", "سعودي", "الرياض"}),
    "UAE": frozenset(
        {"uae", "emirates", "emirati", "dubai", "abu dhabi", "الإمارات", "دبي", "أبوظبي"}
    ),
    "Qatar": frozenset({"qatar", "qatari", "doha", "قطر", "الدوحة"}),
    "Kuwait": frozenset({"kuwait", "kuwaiti", "الكويت"}),
    "Bahrain": frozenset({"bahrain", "bahraini", "المنامة", "البحرين"}),
    "Oman": frozenset({"oman", "omani", "muscat", "عمان", "مسقط"}),
    "Yemen": frozenset({"yemen", "yemeni", "sanaa", "اليمن", "صنعاء"}),
    "Jordan": frozenset({"jordan", "jordanian", "amman", "الأردن", "عمان", "الأردني"}),
    "Lebanon": frozenset({"lebanon", "lebanese", "beirut", "لبنان", "بيروت"}),
    "Syria": frozenset({"syria", "syrian", "damascus", "سوريا", "دمشق"}),
    "Iraq": frozenset({"iraq", "iraqi", "baghdad", "العراق", "بغداد"}),
    "Palestine": frozenset(
        {"palestine", "palestinian", "gaza", "west bank", "فلسطين", "غزة", "الضفة"}
    ),
    "Morocco": frozenset({"morocco", "moroccan", "rabat", "casablanca", "المغرب", "الرباط"}),
    "Algeria": frozenset({"algeria", "algerian", "algiers", "الجزائر", "الجزائرية"}),
    "Tunisia": frozenset({"tunisia", "tunisian", "tunis", "تونس"}),
    "Libya": frozenset({"libya", "libyan", "tripoli", "ليبيا", "طرابلس"}),
    "Sudan": frozenset({"sudan", "sudanese", "khartoum", "السودان", "الخرطوم"}),
    "Turkey": frozenset({"turkey", "turkish", "ankara", "istanbul", "تركيا", "إسطنبول"}),
    "Iran": frozenset({"iran", "iranian", "tehran", "إيران", "طهران"}),
    "MENA": frozenset(
        {
            "mena",
            "middle east",
            "north africa",
            "arab world",
            "gulf",
            "gcc",
            "الشرق الأوسط",
            "شمال أفريقيا",
            "العالم العربي",
            "الخليج",
            "مينا",
        }
    ),
}

_DOMAIN_TOOLS: dict[Domain, list[str]] = {
    Domain.GENERAL: ["tavily", "brave_search", "wikipedia"],
    Domain.ACADEMIC: ["semantic_scholar", "crossref", "arxiv", "tavily"],
    Domain.MEDICAL: ["pubmed", "europe_pmc", "semantic_scholar", "tavily"],
    Domain.NEWS: ["gdelt", "brave_search", "tavily"],
    Domain.TECHNICAL: ["github", "stack_exchange", "tavily", "wikipedia"],
    Domain.MENA_LOCAL: ["brave_search", "tavily", "gdelt", "wikipedia"],
    Domain.LEGAL: ["official_domains", "tavily", "brave_search"],
    Domain.FINANCIAL: ["official_domains", "tavily", "brave_search"],
}

_DOMAIN_CATEGORIES: dict[Domain, list[str]] = {
    Domain.GENERAL: ["web", "wiki"],
    Domain.ACADEMIC: ["academic", "paper", "web"],
    Domain.MEDICAL: ["medical", "academic", "official"],
    Domain.NEWS: ["news", "web"],
    Domain.TECHNICAL: ["technical", "repository", "web"],
    Domain.MENA_LOCAL: ["news", "web", "official"],
    Domain.LEGAL: ["official", "web", "news"],
    Domain.FINANCIAL: ["official", "web", "news"],
}

_DEPTH_SOURCE_BUDGET: dict[Depth, int] = {
    Depth.QUICK: 5,
    Depth.STANDARD: 8,
    Depth.DEEP: 12,
}

_DEPTH_TIME_BUDGET: dict[Depth, int] = {
    Depth.QUICK: 120,
    Depth.STANDARD: 240,
    Depth.DEEP: 300,
}

_DEPTH_MAX_TOOLS: dict[Depth, int] = {
    Depth.QUICK: 2,
    Depth.STANDARD: 4,
    Depth.DEEP: 6,
}

_EN_TO_AR_HINTS: dict[str, str] = {
    "egypt": "مصر",
    "saudi": "السعودية",
    "economy": "اقتصاد",
    "health": "صحة",
    "news": "أخبار",
    "law": "قانون",
    "market": "سوق",
    "bank": "بنك",
    "investment": "استثمار",
    "education": "تعليم",
    "technology": "تقنية",
    "climate": "مناخ",
    "energy": "طاقة",
}

_AR_TO_EN_HINTS: dict[str, str] = {
    "مصر": "Egypt",
    "السعودية": "Saudi Arabia",
    "اقتصاد": "economy",
    "صحة": "health",
    "أخبار": "news",
    "اخبار": "news",
    "قانون": "law",
    "سوق": "market",
    "بنك": "bank",
    "استثمار": "investment",
    "تعليم": "education",
    "تقنية": "technology",
    "مناخ": "climate",
    "طاقة": "energy",
}

_CLARIFICATION_QUESTION = (
    "Could you clarify your research question with more detail "
    "(topic, scope, and time period)? / "
    "هل يمكنك توضيح سؤالك البحثي بمزيد من التفاصيل (الموضوع والنطاق والفترة الزمنية)؟"
)


def detect_language(text: str) -> Language:
    """Detect English, Arabic, or mixed input via the Arabic Unicode blocks (M3.1)."""
    has_arabic = _ARABIC_RE.search(text) is not None
    has_latin = _LATIN_RE.search(text) is not None
    if has_arabic and has_latin:
        return Language.MIXED
    if has_arabic:
        return Language.ARABIC
    return Language.ENGLISH


def _contains_keyword(haystack: str, keywords: frozenset[str]) -> bool:
    """Match keywords on word boundaries to avoid substring false positives.

    ``photosynthesis`` must not match ``thesis`` and ``flaw`` must not match
    ``law``. Multi-word phrases match with boundaries at both ends.
    """
    for keyword in keywords:
        term = keyword.strip().lower()
        if not term:
            continue
        if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", haystack):
            return True
    return False


def classify_domain(text: str) -> Domain:
    """Classify the research domain with deterministic keyword rules (M3.2).

    Priority is high-stakes first (medical, legal, financial), then academic,
    technical, news, and MENA/local. Falls back to general.
    """
    lowered = text.lower()
    if _contains_keyword(lowered, _MEDICAL_KEYWORDS):
        return Domain.MEDICAL
    if _contains_keyword(lowered, _LEGAL_KEYWORDS):
        return Domain.LEGAL
    if _contains_keyword(lowered, _FINANCIAL_KEYWORDS):
        return Domain.FINANCIAL
    if _contains_keyword(lowered, _ACADEMIC_KEYWORDS):
        return Domain.ACADEMIC
    if _contains_keyword(lowered, _TECHNICAL_KEYWORDS):
        return Domain.TECHNICAL
    if _contains_keyword(lowered, _NEWS_KEYWORDS):
        return Domain.NEWS
    _, jurisdictions = detect_locality(text)
    if jurisdictions:
        return Domain.MENA_LOCAL
    return Domain.GENERAL


def detect_locality(text: str) -> tuple[str, list[str]]:
    """Detect locality and jurisdictions (M3.2).

    Returns ``("mena", [...])`` when a MENA entity matches, else ``("global", [])``.
    Jurisdictions are canonical English names in first-match order, deduplicated.
    """
    lowered = text.lower()
    found: list[str] = []
    for canonical, aliases in _MENA_ALIASES.items():
        if canonical == "MENA":
            continue
        if _contains_keyword(lowered, aliases):
            found.append(canonical)
    if _contains_keyword(lowered, _MENA_ALIASES["MENA"]):
        if "MENA" not in found:
            found.append("MENA")
    if found:
        return ("mena", found)
    return ("global", [])


def requires_freshness(text: str) -> bool:
    """Return True when the query needs recent sources (M3.2)."""
    lowered = text.lower()
    return _contains_keyword(lowered, _FRESHNESS_KEYWORDS)


def assess_risk(domain: Domain, text: str) -> RiskLevel:
    """Classify risk level (M3.2): high-stakes for medical/legal/financial."""
    if domain in (Domain.MEDICAL, Domain.LEGAL, Domain.FINANCIAL):
        return RiskLevel.HIGH_STAKES
    lowered = text.lower()
    if _contains_keyword(lowered, _MEDICAL_KEYWORDS | _LEGAL_KEYWORDS | _FINANCIAL_KEYWORDS):
        return RiskLevel.HIGH_STAKES
    return RiskLevel.NORMAL


def _split_parts(query: str) -> list[str]:
    """Split a query into deterministic parts on ?, !, ;, Arabic delimiters, and."""
    normalized = query.replace("؟", "?").replace("؛", ";")
    chunks = re.split(r"[?!;]+|\n+", normalized)
    parts: list[str] = []
    for chunk in chunks:
        for sub in re.split(r"\s+(?:and|or|vs\.?|versus)\s+|\s+و\s+", chunk, flags=re.IGNORECASE):
            cleaned = sub.strip(" \t\r\n-–—:,.")
            if len(cleaned) >= 3:
                parts.append(cleaned)
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        key = part.lower()
        if key not in seen:
            seen.add(key)
            unique.append(part)
    return unique


def select_depth(
    query: str,
    domain: Domain,
    risk: RiskLevel,
    freshness: bool,
    locality: str = "global",
) -> Depth:
    """Apply deterministic quick / standard / deep rules (M3.3)."""
    text = query.strip()
    lowered = text.lower()
    words = _WORD_RE.findall(text)
    parts = _split_parts(text)

    if risk == RiskLevel.HIGH_STAKES:
        return Depth.DEEP
    if domain == Domain.ACADEMIC:
        return Depth.DEEP
    if domain == Domain.MENA_LOCAL or locality == "mena":
        # Regional research needs broader bilingual coverage; deep when
        # multi-part, comparative, contested, fresh, or non-trivial in size.
        if (
            len(parts) >= 2
            or len(words) > 12
            or freshness
            or _contains_keyword(lowered, _CONTESTED_KEYWORDS | _COMPARISON_KEYWORDS)
            or lowered.count("?") >= 1
        ):
            return Depth.DEEP
    if len(parts) >= 3 or lowered.count("?") >= 2:
        return Depth.DEEP
    if len(words) > 40 or len(text) > 300:
        return Depth.DEEP
    if _contains_keyword(lowered, _CONTESTED_KEYWORDS):
        return Depth.DEEP

    is_comparison = _contains_keyword(lowered, _COMPARISON_KEYWORDS)
    if (
        len(words) <= 12
        and len(text) <= 120
        and len(parts) <= 1
        and lowered.count("?") <= 1
        and not freshness
        and not is_comparison
    ):
        return Depth.QUICK
    return Depth.STANDARD


def build_subquestions(query: str, language: Language, *, max_items: int = 4) -> list[str]:
    """Generate focused deterministic subquestions (M3.4)."""
    cleaned = " ".join(query.strip().split())
    parts = _split_parts(cleaned)
    if len(parts) >= 2:
        return parts[:max_items]
    if language == Language.ARABIC:
        expansions = [
            cleaned,
            f"{cleaned} — حقائق أساسية وخلفية",
            f"{cleaned} — أدلة ومصادر حديثة",
        ]
    elif language == Language.MIXED:
        expansions = [
            cleaned,
            f"{cleaned} — key facts and background",
            f"{cleaned} — حقائق أساسية وأدلة داعمة",
        ]
    else:
        expansions = [
            cleaned,
            f"{cleaned} — key facts and background",
            f"{cleaned} — recent evidence and sources",
        ]
    seen: set[str] = set()
    out: list[str] = []
    for item in expansions:
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
        if len(out) >= max_items:
            break
    return out


def _synthesize_bilingual_hint(query: str, target: str) -> str:
    """Build a deterministic opposite-script hint variant for MENA coverage."""
    if target == "ar":
        hinted = query
        for en, ar in _EN_TO_AR_HINTS.items():
            hinted = re.sub(r"(?<!\w)" + re.escape(en) + r"(?!\w)", ar, hinted, flags=re.IGNORECASE)
        if _ARABIC_RE.search(hinted) is None:
            hinted = f"{query} أخبار ومصادر"
        return hinted
    hinted_en = query
    for ar, en in _AR_TO_EN_HINTS.items():
        hinted_en = hinted_en.replace(ar, en)
    if _LATIN_RE.search(hinted_en) is None:
        hinted_en = f"{query} news sources"
    elif hinted_en == query:
        hinted_en = f"{query} news and sources"
    return hinted_en


def build_query_variants(
    query: str,
    language: Language,
    subquestions: list[str],
    domain: Domain,
    locality: str,
) -> list[str]:
    """Build deduplicated search variants, bilingual when useful (M3.4)."""
    cleaned = " ".join(query.strip().split())
    candidates: list[str] = [cleaned, *subquestions]
    seen: set[str] = set()
    variants: list[str] = []
    for candidate in candidates:
        normalized = " ".join(candidate.split())
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            variants.append(normalized)
        if len(variants) >= 6:
            break
    needs_bilingual = (
        domain == Domain.MENA_LOCAL or locality == "mena" or language == Language.MIXED
    )
    if needs_bilingual and variants:
        blob = " ".join(variants)
        has_arabic = _ARABIC_RE.search(blob) is not None
        has_latin = _LATIN_RE.search(blob) is not None
        if not has_arabic and len(variants) < 6:
            hint = _synthesize_bilingual_hint(cleaned, "ar")
            if hint.lower() not in seen:
                variants.append(hint)
        elif not has_latin and len(variants) < 6:
            hint = _synthesize_bilingual_hint(cleaned, "en")
            if hint.lower() not in seen:
                variants.append(hint)
    return variants[:6]


def select_tools(domain: Domain, depth: Depth) -> tuple[list[str], list[str]]:
    """Select tools and source categories within the depth budget (M3.5)."""
    tools = list(_DOMAIN_TOOLS.get(domain, _DOMAIN_TOOLS[Domain.GENERAL]))
    categories = list(_DOMAIN_CATEGORIES.get(domain, _DOMAIN_CATEGORIES[Domain.GENERAL]))
    return (tools[: _DEPTH_MAX_TOOLS[depth]], categories)


def is_ambiguous(query: str) -> bool:
    """Return True when the request is too vague for a reliable plan (M3.6)."""
    cleaned = query.strip()
    if not cleaned:
        return True
    if _URL_RE.search(cleaned) is not None:
        return False
    lowered = cleaned.lower()
    if lowered in _VAGUE_QUERIES:
        return True
    words = _WORD_RE.findall(cleaned)
    if len(cleaned) < 10 or (len(words) <= 2 and len(cleaned) < 20):
        return True
    return False


def analyze_query(query: str) -> ResearchPlan:
    """Analyze a raw query string into a validated ResearchPlan (M3.7)."""
    cleaned = " ".join(query.strip().split())
    if not cleaned:
        raise ValueError("Query must not be empty.")
    if _URL_RE.search(cleaned) is not None:
        # Provided-URL research: keep the URL as the topic and prefer direct fetch.
        language = detect_language(_URL_RE.sub("", cleaned))
        domain = Domain.GENERAL
        locality, jurisdictions = detect_locality(cleaned)
        freshness = False
        risk = RiskLevel.NORMAL
        depth = Depth.STANDARD
        subquestions = [cleaned]
        variants = [cleaned]
        tools = ["official_domains", "tavily"]
        categories = ["web", "official"]
        return ResearchPlan(
            query=cleaned,
            language=language,
            domain=domain,
            locality=locality,
            jurisdictions=jurisdictions,
            requires_freshness=freshness,
            risk_level=risk,
            depth=depth,
            subquestions=subquestions,
            query_variants=variants,
            tools_selected=tools,
            source_categories=categories,
            source_budget=_DEPTH_SOURCE_BUDGET[depth],
            time_budget_seconds=_DEPTH_TIME_BUDGET[depth],
            needs_clarification=False,
            clarification_question=None,
        )

    language = detect_language(cleaned)
    domain = classify_domain(cleaned)
    locality, jurisdictions = detect_locality(cleaned)
    # A MENA-anchored query without a stronger specialist domain is regional research.
    if jurisdictions and domain == Domain.GENERAL:
        domain = Domain.MENA_LOCAL
    freshness = requires_freshness(cleaned)
    risk = assess_risk(domain, cleaned)
    depth = select_depth(cleaned, domain, risk, freshness, locality)
    subquestions = build_subquestions(cleaned, language)
    variants = build_query_variants(cleaned, language, subquestions, domain, locality)
    tools_selected, categories = select_tools(domain, depth)

    if is_ambiguous(cleaned):
        return ResearchPlan(
            query=cleaned,
            language=language,
            domain=domain,
            locality=locality,
            jurisdictions=jurisdictions,
            requires_freshness=freshness,
            risk_level=risk,
            depth=depth,
            subquestions=[cleaned] if not subquestions else subquestions[:1],
            query_variants=[cleaned] if not variants else variants[:1],
            tools_selected=tools_selected,
            source_categories=categories,
            source_budget=_DEPTH_SOURCE_BUDGET[depth],
            time_budget_seconds=_DEPTH_TIME_BUDGET[depth],
            needs_clarification=True,
            clarification_question=_CLARIFICATION_QUESTION,
        )

    return ResearchPlan(
        query=cleaned,
        language=language,
        domain=domain,
        locality=locality,
        jurisdictions=jurisdictions,
        requires_freshness=freshness,
        risk_level=risk,
        depth=depth,
        subquestions=subquestions,
        query_variants=variants,
        tools_selected=tools_selected,
        source_categories=categories,
        source_budget=_DEPTH_SOURCE_BUDGET[depth],
        time_budget_seconds=_DEPTH_TIME_BUDGET[depth],
        needs_clarification=False,
        clarification_question=None,
    )


def analyze_request(request: ResearchRequest) -> ResearchPlan:
    """Analyze a validated ResearchRequest into a validated ResearchPlan."""
    text = request.query.strip()
    if request.source_url is not None:
        text = f"{text} {request.source_url}".strip()
    plan = analyze_query(text)
    # Preserve the caller-declared language hint only when detection saw English
    # but the session explicitly prefers Arabic (or vice versa); detection wins
    # for mixed content so bilingual expansion is not lost.
    if plan.language == Language.ENGLISH and request.language == Language.ARABIC:
        plan = plan.model_copy(update={"language": Language.ARABIC})
    elif plan.language == Language.ARABIC and request.language == Language.ENGLISH:
        plan = plan.model_copy(update={"language": Language.ENGLISH})
    return plan
