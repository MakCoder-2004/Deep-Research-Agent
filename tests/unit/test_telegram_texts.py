"""Unit tests for localized Telegram strings (M2.5)."""

from __future__ import annotations

from research_agent.telegram.texts import (
    UNAUTHORIZED_AR,
    UNAUTHORIZED_EN,
    pick_unauthorized,
    render_help,
    render_start,
    render_whoami,
)


def test_pick_unauthorized_english_by_default() -> None:
    assert pick_unauthorized(None) == UNAUTHORIZED_EN
    assert pick_unauthorized("en") == UNAUTHORIZED_EN
    assert pick_unauthorized("fr") == UNAUTHORIZED_EN
    assert pick_unauthorized("") == UNAUTHORIZED_EN


def test_pick_unauthorized_arabic() -> None:
    assert pick_unauthorized("ar") == UNAUTHORIZED_AR
    assert pick_unauthorized("ar-EG") == UNAUTHORIZED_AR
    assert pick_unauthorized("AR") == UNAUTHORIZED_AR


def test_unauthorized_reveals_no_config_details() -> None:
    for text in (UNAUTHORIZED_EN, UNAUTHORIZED_AR):
        assert "TELEGRAM_" not in text
        assert "TOKEN" not in text
        assert "ALLOWED" not in text
        assert not any(ch.isdigit() for ch in text)


def test_render_whoami_exact_wording() -> None:
    assert render_whoami(123456789) == "Your Telegram user ID is: 123456789"


def test_limits_are_truthful_until_daily_quotas_are_implemented() -> None:
    english = render_start("en") + render_help("en")
    arabic = render_start("ar") + render_help("ar")
    assert english.count("3 concurrent jobs globally") == 2
    assert english.count("1 active job per user") == 2
    assert english.count("Daily quotas coming soon (M9).") == 2
    assert "10 requests per user" not in english
    assert "3 مهام متزامنة عالميًا" in arabic
    assert "مهمة نشطة واحدة لكل مستخدم" in arabic
    assert arabic.count("الحصص اليومية قادمة قريبًا (M9).") == 2
