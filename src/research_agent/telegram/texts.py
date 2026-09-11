"""Localized Telegram strings (English and Arabic)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, cast

WHOAMI_TEMPLATE = "Your Telegram user ID is: {user_id}"


class _StringKeyed(Protocol):
    """Protocol for SQLite rows and other string-keyed payloads."""

    def __getitem__(self, key: str, /) -> object:
        ...


def _get_field(item: object, key: str) -> object | None:
    """Read a field from mappings, SQLite rows, or attribute objects."""
    if isinstance(item, Mapping):
        return cast(Mapping[str, object], item).get(key)
    try:
        return cast(_StringKeyed, item)[key]
    except (KeyError, IndexError, TypeError):
        pass
    try:
        return cast(object | None, getattr(item, key, None))
    except Exception:  # noqa: BLE001, S110 - malformed rows are skipped
        return None

START_EN = (
    "Welcome to Deep Research Agent.\n\n"
    "Send me a research question or an https:// link and I will research it "
    "for you in English or Arabic.\n\n"
    "Commands:\n"
    "/research <query> - start a new research job\n"
    "/status - show queue position and current stage\n"
    "/history - show your recent reports\n"
    "/report <id> - retrieve a prior report\n"
    "/cancel - cancel your active job\n"
    "/forget - clear your temporary session\n"
    "/language - choose English or Arabic\n"
    "/help - show examples and limits\n"
    "/whoami - show your numeric Telegram ID\n\n"
    "Plain text works like /research.\n\n"
    "Limits: trusted users only (numeric allowlist); 3 concurrent jobs globally "
    "with 1 active job per user; 300s job timeout with 1 repair cycle; "
    "reports kept 90 days; sessions kept 24h or last 6 interactions.\n"
    "Daily quotas coming soon (M9)."
)

START_AR = (
    "مرحبًا بك في وكيل البحث العميق.\n\n"
    "أرسل لي سؤالًا بحثيًا أو رابط https:// وسأبحث عنه لك بالعربية أو الإنجليزية.\n\n"
    "الأوامر:\n"
    "/research <استعلام> - بدء مهمة بحث جديدة\n"
    "/status - عرض موضعك في قائمة الانتظار ومرحلة التنفيذ\n"
    "/history - عرض تقاريرك الأخيرة\n"
    "/report <id> - استرجاع تقرير سابق\n"
    "/cancel - إلغاء مهمتك النشطة\n"
    "/forget - مسح جلستك المؤقتة\n"
    "/language - اختيار العربية أو الإنجليزية\n"
    "/help - عرض الأمثلة والحدود\n"
    "/whoami - عرض معرّف تيليجرام الرقمي الخاص بك\n\n"
    "النص العادي يعمل مثل /research.\n\n"
    "الحدود: للمستخدمين الموثوقين فقط (قائمة سماح رقمية)؛ 3 مهام متزامنة عالميًا "
    "مع مهمة نشطة واحدة لكل مستخدم؛ مهلة 300 ثانية مع دورة إصلاح واحدة؛ "
    "تُحفظ التقارير 90 يومًا؛ وتُحفظ الجلسات 24 ساعة أو آخر 6 تفاعلات.\n"
    "الحصص اليومية قادمة قريبًا (M9)."
)

HELP_EN = (
    "How to use Deep Research Agent.\n\n"
    "Examples:\n"
    "/research What are recent advances in solid-state batteries?\n"
    "/research https://example.com/article-about-ai\n"
    "What are the latest developments in solar energy? (plain text also works)\n\n"
    "Commands:\n"
    "/research <query> - start a new research job\n"
    "/status - show queue position and current stage\n"
    "/history - show your recent reports\n"
    "/report <id> - retrieve a prior report\n"
    "/cancel - cancel your active job\n"
    "/forget - clear your temporary session\n"
    "/language <en|ar> - choose English or Arabic\n"
    "/whoami - show your numeric Telegram ID\n\n"
    "Limits: trusted users only; 3 concurrent jobs globally "
    "with 1 active job per user; 300s job timeout with 1 repair cycle; "
    "reports kept 90 days; sessions kept 24h or last 6 interactions.\n"
    "Daily quotas coming soon (M9)."
)

HELP_AR = (
    "كيفية استخدام وكيل البحث العميق.\n\n"
    "أمثلة:\n"
    "/research ما هي أحدث التطورات في بطاريات الحالة الصلبة؟\n"
    "/research https://example.com/article-about-ai\n"
    "ما هي آخر التطورات في الطاقة الشمسية؟ (النص العادي يعمل أيضًا)\n\n"
    "الأوامر:\n"
    "/research <استعلام> - بدء مهمة بحث جديدة\n"
    "/status - عرض موضعك في قائمة الانتظار ومرحلة التنفيذ\n"
    "/history - عرض تقاريرك الأخيرة\n"
    "/report <id> - استرجاع تقرير سابق\n"
    "/cancel - إلغاء مهمتك النشطة\n"
    "/forget - مسح جلستك المؤقتة\n"
    "/language <en|ar> - اختيار العربية أو الإنجليزية\n"
    "/whoami - عرض معرّف تيليجرام الرقمي الخاص بك\n\n"
    "الحدود: للمستخدمين الموثوقين فقط؛ 3 مهام متزامنة عالميًا "
    "مع مهمة نشطة واحدة لكل مستخدم؛ مهلة 300 ثانية مع دورة إصلاح واحدة؛ "
    "تُحفظ التقارير 90 يومًا؛ وتُحفظ الجلسات 24 ساعة أو آخر 6 تفاعلات.\n"
    "الحصص اليومية قادمة قريبًا (M9)."
)


def render_whoami(user_id: int) -> str:
    """Render the /whoami reply with the exact required wording."""
    return WHOAMI_TEMPLATE.format(user_id=user_id)


UNAUTHORIZED_EN = (
    "Sorry, you don't have access to this bot. "
    "Use /whoami to get your user ID and ask the administrator to grant you access."
)
UNAUTHORIZED_AR = (
    "عذرًا، ليس لديك صلاحية استخدام هذا البوت. "
    "استخدم /whoami لمعرفة المعرّف الخاص بك واطلب من المسؤول منحك حق الوصول."
)

UNAVAILABLE_EN = "Service temporarily unavailable. Please try again later."
UNAVAILABLE_AR = "الخدمة غير متاحة مؤقتًا. يرجى المحاولة لاحقًا."


def pick_lang(lang_code: str | None) -> str:
    """Pick 'ar' for Arabic language codes, otherwise 'en'."""
    if lang_code and lang_code.lower().startswith("ar"):
        return "ar"
    return "en"


# Shared localized-text table: all static EN/AR switching is resolved through
# one table rather than mutating a collection of lazy per-function maps.
_T: dict[str, dict[str, str]] = {}


def _t(key: str, lang_code: str | None) -> str:
    """Return the localized string for ``key`` via :func:`pick_lang`."""
    lang = pick_lang(lang_code)
    entry = _T.get(key, {})
    text = entry.get(lang)
    if text is None:
        text = entry.get("en", "")
    return text


def pick_unauthorized(lang_code: str | None) -> str:
    """Pick the denial reply matching the user's language (Arabic or English)."""
    return _t("unauthorized", lang_code)


def render_unavailable(lang_code: str | None) -> str:
    """Render the service-unavailable reply (no job created, no fake ID)."""
    return _t("unavailable", lang_code)


def render_start(lang_code: str | None) -> str:
    """Render the /start capability and restriction message."""
    return _t("start", lang_code)


def render_help(lang_code: str | None) -> str:
    """Render the /help examples and limits message."""
    return _t("help", lang_code)


RESEARCH_USAGE_EN = (
    "Please provide a research question or link. "
    "Usage: /research <query> - e.g. /research What are recent advances "
    "in solar batteries? Plain text also works like /research."
)

RESEARCH_USAGE_AR = (
    "يرجى تقديم سؤال بحثي أو رابط. "
    "الاستخدام: /research <استعلام> - مثال: /research ما هي أحدث التطورات "
    "في البطاريات الشمسية؟ النص العادي يعمل أيضًا مثل /research."
)


def render_research_usage(lang_code: str | None) -> str:
    """Render the /research usage reply (no job created)."""
    return _t("research_usage", lang_code)


def render_research_accepted(query: str, job_id: str, lang_code: str | None) -> str:
    """Render the research accepted reply, preserving the query verbatim."""
    if pick_lang(lang_code) == "ar":
        return (
            f"تم استلام طلب البحث: {query}\nمعرّف المهمة: {job_id}\nاستخدم /status لمتابعة التقدم."
        )
    return f"Research request received: {query}\nJob ID: {job_id}\nUse /status to check progress."


NON_TEXT_EN = (
    "I can only handle text questions or links. Please send text or use /research <query>."
)

NON_TEXT_AR = (
    "يمكنني التعامل مع الأسئلة النصية أو الروابط فقط. يرجى إرسال نص أو استخدام /research <استعلام>."
)


def render_non_text(lang_code: str | None) -> str:
    """Render the gentle reply for non-text messages (no job created)."""
    return _t("non_text", lang_code)


INVALID_URL_EN = (
    "That link looks invalid. Please send an http(s) URL without credentials "
    "(e.g. https://example.com/article) or send a text question instead."
)

INVALID_URL_AR = (
    "يبدو أن هذا الرابط غير صالح. يرجى إرسال رابط http(s) بدون بيانات اعتماد "
    "(مثال: https://example.com/article) أو إرسال سؤال نصي بدلًا من ذلك."
)


def render_invalid_url(lang_code: str | None) -> str:
    """Render the invalid-URL reply (no job created)."""
    return _t("invalid_url", lang_code)


STATUS_NONE_EN = "You have no active research job. Send /research <query> to start one."

STATUS_NONE_AR = "ليس لديك مهمة بحث نشطة. أرسل /research <استعلام> لبدء مهمة."


def format_status(
    state: str,
    position: int | None = None,
    stage: str | None = None,
    lang_code: str | None = None,
    job_id: str | None = None,
    query: str | None = None,
) -> str:
    """Format /status replies for none/queued #N/active stage cases."""
    lang = pick_lang(lang_code)
    normalized = (state or "none").lower()
    if normalized == "queued":
        pos = position if position and position > 0 else 1
        if lang == "ar":
            base = f"مهمتك في قائمة الانتظار بالموضع #{pos}."
            if job_id:
                base += f"\nمعرّف المهمة: {job_id}"
            if query:
                base += f"\nالاستعلام: {query}"
            base += "\nاستخدم /cancel للإلغاء."
            return base
        base = f"Your research job is queued at position #{pos}."
        if job_id:
            base += f"\nJob ID: {job_id}"
        if query:
            base += f"\nQuery: {query}"
        base += "\nUse /cancel to cancel."
        return base
    if normalized == "active":
        current_stage = stage or "active"
        if lang == "ar":
            base = f"مهمتك نشطة الآن (المرحلة: {current_stage})."
            if job_id:
                base += f"\nمعرّف المهمة: {job_id}"
            if query:
                base += f"\nالاستعلام: {query}"
            return base
        base = f"Your research job is active (stage: {current_stage})."
        if job_id:
            base += f"\nJob ID: {job_id}"
        if query:
            base += f"\nQuery: {query}"
        return base
    return _t("status_none", lang_code)


CANCELLED_EN_TEMPLATE = "Your active research job {job_id} has been cancelled."
CANCELLED_AR_TEMPLATE = "تم إلغاء مهمة البحث النشطة {job_id}."
CANCEL_NONE_EN = "You have no active research job to cancel."
CANCEL_NONE_AR = "ليس لديك مهمة بحث نشطة لإلغائها."


def render_cancelled(job_id: str, lang_code: str | None) -> str:
    """Render the cancellation confirmation, preserving the job ID verbatim."""
    return _t("cancelled", lang_code).format(job_id=job_id)


def render_cancel_none(lang_code: str | None) -> str:
    """Render the no-active-job reply for /cancel."""
    return _t("cancel_none", lang_code)


BUSY_EN_TEMPLATE = "You already have active job {job_id}. Use /status or /cancel."
BUSY_NO_ID_EN = "You already have active job. Use /status or /cancel."
BUSY_AR_TEMPLATE = (
    "لديك بالفعل مهمة نشطة {job_id}. استخدم /status لمتابعة التقدم أو /cancel للإلغاء."
)
BUSY_NO_ID_AR = "لديك بالفعل مهمة نشطة. استخدم /status لمتابعة التقدم أو /cancel للإلغاء."


def render_busy(job_id: str | None, lang_code: str | None) -> str:
    """Render the fast-reject reply when the user already has an active job."""
    lang = pick_lang(lang_code)
    if lang == "ar":
        if job_id:
            return BUSY_AR_TEMPLATE.format(job_id=job_id)
        return BUSY_NO_ID_AR
    if job_id:
        return BUSY_EN_TEMPLATE.format(job_id=job_id)
    return BUSY_NO_ID_EN


HISTORY_EMPTY_EN = "You have no saved reports yet. Send /research <query> to create one."
HISTORY_EMPTY_AR = "لا توجد لديك تقارير محفوظة بعد. أرسل /research <استعلام> لإنشاء تقرير."
HISTORY_HEADER_EN = "Your recent reports (last {n}):"
HISTORY_HEADER_AR = "تقاريرك الأخيرة (آخر {n}):"
REPORT_USAGE_EN = "Usage: /report <id> - e.g. /report abc123. Use /history to list reports."
REPORT_USAGE_AR = "الاستخدام: /report <id> - مثال: /report abc123. استخدم /history لعرض التقارير."
REPORT_NOT_FOUND_EN = "Report not found. Use /history to list your recent reports."
REPORT_NOT_FOUND_AR = "التقرير غير موجود. استخدم /history لعرض تقاريرك الأخيرة."


def format_history(reports: Sequence[object] | None, lang_code: str | None) -> str:
    """Format the /history reply for the last reports (owner-scoped)."""
    items = list(reports) if reports else []
    lang = pick_lang(lang_code)
    if not items:
        return _t("history_empty", lang_code)
    header = _t("history_header", lang_code).format(n=len(items))
    lines = [header]
    for row in items:
        report_id = _get_field(row, "report_id")
        topic = _get_field(row, "topic")
        if report_id is None or topic is None:
            continue
        lines.append(f"- {report_id}: {topic}")
    lines.append(
        "استخدم /report <id> لاسترجاع تقرير."
        if lang == "ar"
        else "Use /report <id> to retrieve a report."
    )
    return "\n".join(lines)


def render_report_usage(lang_code: str | None) -> str:
    """Render the /report usage reply (no lookup performed)."""
    return _t("report_usage", lang_code)


def render_report_not_found(lang_code: str | None) -> str:
    """Render the unknown-or-unowned report reply."""
    return _t("report_not_found", lang_code)


def format_report_bundle(
    report: object, sources: Sequence[object] | None, lang_code: str | None
) -> str:
    """Format a retrieved report with readable [1] citation markers."""
    lang = pick_lang(lang_code)
    topic = _get_field(report, "topic")
    summary = _get_field(report, "summary")
    report_id = _get_field(report, "report_id")
    if topic is None or summary is None or report_id is None:
        return render_report_not_found(lang_code)
    src_list = list(sources) if sources else []
    if lang == "ar":
        lines = [f"التقرير: {topic}", f"المعرّف: {report_id}", "", str(summary), "", "المصادر:"]
    else:
        lines = [f"Report: {topic}", f"ID: {report_id}", "", str(summary), "", "Sources:"]
    for idx, src in enumerate(src_list, start=1):
        title = _get_field(src, "title")
        url = _get_field(src, "url")
        if title is None or url is None:
            continue
        lines.append(f"[{idx}] {title} - {url}")
    return "\n".join(lines)


FORGET_DONE_EN = "Your temporary session has been cleared. Your jobs and reports are kept."
FORGET_DONE_AR = "تم مسح جلستك المؤقتة. تم الاحتفاظ بمهامك وتقاريرك."


def render_forget_done(lang_code: str | None) -> str:
    """Render the /forget confirmation (jobs and reports are kept)."""
    return _t("forget_done", lang_code)


LANGUAGE_USAGE_EN = "Usage: /language <en|ar> - e.g. /language en or /language ar."
LANGUAGE_USAGE_AR = "الاستخدام: /language <en|ar> - مثال: /language ar أو /language en."
LANGUAGE_INVALID_EN = "Invalid language. Usage: /language <en|ar>."
LANGUAGE_INVALID_AR = "لغة غير صالحة. الاستخدام: /language <en|ar>."


def render_language_usage(lang_code: str | None) -> str:
    """Render the /language usage reply."""
    return _t("language_usage", lang_code)


def render_language_invalid(lang_code: str | None) -> str:
    """Render the invalid-argument reply for /language."""
    return _t("language_invalid", lang_code)


def render_language_current(current: str, lang_code: str | None) -> str:
    """Render the bare /language reply with current preference plus usage."""
    lang = pick_lang(lang_code)
    code = "ar" if current == "ar" else "en"
    if lang == "ar":
        label = "العربية" if code == "ar" else "الإنجليزية"
        return f"لغتك الحالية هي {label} ({code}). {LANGUAGE_USAGE_AR}"
    label = "Arabic" if code == "ar" else "English"
    return f"Your current language is {label} ({code}). {LANGUAGE_USAGE_EN}"


def render_language_set(new_code: str, lang_code: str | None) -> str:
    """Render the /language confirmation, preserving the new code verbatim."""
    code = "ar" if new_code == "ar" else "en"
    if pick_lang(lang_code) == "ar" or code == "ar":
        # Reply in Arabic when the new preference is Arabic for immediate feedback.
        if code == "ar":
            return "تم ضبط اللغة إلى العربية (ar)."
        return "Language set to English (en)."
    return "Language set to English (en)."


MEDICAL_DISCLAIMER_EN = (
    "Medical disclaimer: this research is for information only and is not medical advice. "
    "Consult a qualified professional."
)
MEDICAL_DISCLAIMER_AR = (
    "تنبيه طبي: هذا البحث لأغراض معلوماتية فقط وليس استشارة طبية. استشر مختصًا مؤهلًا."
)
LEGAL_DISCLAIMER_EN = (
    "Legal disclaimer: this research is for information only and is not legal advice. "
    "Consult a qualified professional."
)
LEGAL_DISCLAIMER_AR = (
    "تنبيه قانوني: هذا البحث لأغراض معلوماتية فقط وليس استشارة قانونية. استشر مختصًا مؤهلًا."
)
FINANCIAL_DISCLAIMER_EN = (
    "Financial disclaimer: this research is for information only and is not financial advice. "
    "Consult a qualified professional."
)
FINANCIAL_DISCLAIMER_AR = (
    "تنبيه مالي: هذا البحث لأغراض معلوماتية فقط وليس استشارة مالية. استشر مختصًا مؤهلًا."
)


# Populate the complete table after every static string has been declared.
_T.update(
    {
        "unauthorized": {"en": UNAUTHORIZED_EN, "ar": UNAUTHORIZED_AR},
        "unavailable": {"en": UNAVAILABLE_EN, "ar": UNAVAILABLE_AR},
        "start": {"en": START_EN, "ar": START_AR},
        "help": {"en": HELP_EN, "ar": HELP_AR},
        "research_usage": {"en": RESEARCH_USAGE_EN, "ar": RESEARCH_USAGE_AR},
        "non_text": {"en": NON_TEXT_EN, "ar": NON_TEXT_AR},
        "invalid_url": {"en": INVALID_URL_EN, "ar": INVALID_URL_AR},
        "status_none": {"en": STATUS_NONE_EN, "ar": STATUS_NONE_AR},
        "cancelled": {"en": CANCELLED_EN_TEMPLATE, "ar": CANCELLED_AR_TEMPLATE},
        "cancel_none": {"en": CANCEL_NONE_EN, "ar": CANCEL_NONE_AR},
        "history_empty": {"en": HISTORY_EMPTY_EN, "ar": HISTORY_EMPTY_AR},
        "history_header": {"en": HISTORY_HEADER_EN, "ar": HISTORY_HEADER_AR},
        "report_usage": {"en": REPORT_USAGE_EN, "ar": REPORT_USAGE_AR},
        "report_not_found": {"en": REPORT_NOT_FOUND_EN, "ar": REPORT_NOT_FOUND_AR},
        "forget_done": {"en": FORGET_DONE_EN, "ar": FORGET_DONE_AR},
        "language_usage": {"en": LANGUAGE_USAGE_EN, "ar": LANGUAGE_USAGE_AR},
        "language_invalid": {"en": LANGUAGE_INVALID_EN, "ar": LANGUAGE_INVALID_AR},
    }
)


_HIGH_STAKES_TABLE: dict[str, dict[str, str]] = {
    "medical": {"en": MEDICAL_DISCLAIMER_EN, "ar": MEDICAL_DISCLAIMER_AR},
    "legal": {"en": LEGAL_DISCLAIMER_EN, "ar": LEGAL_DISCLAIMER_AR},
    "financial": {"en": FINANCIAL_DISCLAIMER_EN, "ar": FINANCIAL_DISCLAIMER_AR},
}


def render_high_stakes_disclaimer(domain: str | None, lang_code: str | None) -> str | None:
    """Return the medical/legal/financial disclaimer for a domain, if any.

    Returns ``None`` for general domains so callers only attach a disclaimer
    when required. Language is resolved once via :func:`pick_lang`.
    """
    if not domain:
        return None
    entry = _HIGH_STAKES_TABLE.get(domain.strip().lower())
    if entry is None:
        return None
    lang = pick_lang(lang_code)
    return entry.get(lang, entry["en"])
