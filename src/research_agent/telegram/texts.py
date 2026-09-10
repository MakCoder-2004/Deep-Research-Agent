"""Localized Telegram strings (English and Arabic)."""

from __future__ import annotations

WHOAMI_TEMPLATE = "Your Telegram user ID is: {user_id}"

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
    "Limits: trusted users only (numeric allowlist); up to 10 requests per user "
    "per day (max 3 deep); 3 concurrent jobs globally with 1 active job per user; "
    "300s job timeout with 1 repair cycle; reports kept 90 days; "
    "sessions kept 24h or last 6 interactions."
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
    "الحدود: للمستخدمين الموثوقين فقط (قائمة سماح رقمية)؛ حتى 10 طلبات يوميًا "
    "لكل مستخدم (بحد أقصى 3 معمّقة)؛ 3 مهام متزامنة عالميًا مع مهمة نشطة واحدة "
    "لكل مستخدم؛ مهلة 300 ثانية مع دورة إصلاح واحدة؛ تُحفظ التقارير 90 يومًا؛ "
    "وتُحفظ الجلسات 24 ساعة أو آخر 6 تفاعلات."
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
    "Limits: trusted users only; up to 10 requests per user per day "
    "(max 3 deep); 3 concurrent jobs globally with 1 active job per user; "
    "300s job timeout with 1 repair cycle; reports kept 90 days; "
    "sessions kept 24h or last 6 interactions."
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
    "الحدود: للمستخدمين الموثوقين فقط؛ حتى 10 طلبات يوميًا لكل مستخدم "
    "(بحد أقصى 3 معمّقة)؛ 3 مهام متزامنة عالميًا مع مهمة نشطة واحدة لكل مستخدم؛ "
    "مهلة 300 ثانية مع دورة إصلاح واحدة؛ تُحفظ التقارير 90 يومًا؛ "
    "وتُحفظ الجلسات 24 ساعة أو آخر 6 تفاعلات."
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


def pick_unauthorized(lang_code: str | None) -> str:
    """Pick the denial reply matching the user's language (Arabic or English)."""
    if lang_code and lang_code.lower().startswith("ar"):
        return UNAUTHORIZED_AR
    return UNAUTHORIZED_EN


def pick_lang(lang_code: str | None) -> str:
    """Pick 'ar' for Arabic language codes, otherwise 'en'."""
    if lang_code and lang_code.lower().startswith("ar"):
        return "ar"
    return "en"


def render_start(lang_code: str | None) -> str:
    """Render the /start capability and restriction message."""
    return START_AR if pick_lang(lang_code) == "ar" else START_EN


def render_help(lang_code: str | None) -> str:
    """Render the /help examples and limits message."""
    return HELP_AR if pick_lang(lang_code) == "ar" else HELP_EN


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
    return RESEARCH_USAGE_AR if pick_lang(lang_code) == "ar" else RESEARCH_USAGE_EN


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
    return NON_TEXT_AR if pick_lang(lang_code) == "ar" else NON_TEXT_EN


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
    return INVALID_URL_AR if pick_lang(lang_code) == "ar" else INVALID_URL_EN


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
    return STATUS_NONE_AR if lang == "ar" else STATUS_NONE_EN


CANCELLED_EN_TEMPLATE = "Your active research job {job_id} has been cancelled."
CANCELLED_AR_TEMPLATE = "تم إلغاء مهمة البحث النشطة {job_id}."
CANCEL_NONE_EN = "You have no active research job to cancel."
CANCEL_NONE_AR = "ليس لديك مهمة بحث نشطة لإلغائها."


def render_cancelled(job_id: str, lang_code: str | None) -> str:
    """Render the cancellation confirmation, preserving the job ID verbatim."""
    if pick_lang(lang_code) == "ar":
        return CANCELLED_AR_TEMPLATE.format(job_id=job_id)
    return CANCELLED_EN_TEMPLATE.format(job_id=job_id)


def render_cancel_none(lang_code: str | None) -> str:
    """Render the no-active-job reply for /cancel."""
    return CANCEL_NONE_AR if pick_lang(lang_code) == "ar" else CANCEL_NONE_EN
