"""Localized Telegram strings (English and Arabic)."""

from __future__ import annotations

WHOAMI_TEMPLATE = "Your Telegram user ID is: {user_id}"

START_EN = (
    "Send me a research question or a link and I will research it for you. "
    "Use /whoami to get your user ID."
)
START_AR = "أرسل لي سؤالًا بحثيًا أو رابطًا وسأبحث عنه لك. استخدم /whoami لمعرفة المعرّف الخاص بك."


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
