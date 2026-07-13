import pytest
from bank_audit.loophole.chat.tools_nanobot import NANOBOT_TOOLS, web_fetch, web_search


def test_nanobot_tools_have_unique_names():
    names = [cls().name for cls in NANOBOT_TOOLS]
    assert len(names) == len(set(names))
    assert "audit_web_search" in names
    assert "audit_db_query" in names


def test_web_search_returns_empty_for_empty_query():
    assert web_search("") == []


def test_web_fetch_with_bad_url_returns_none(monkeypatch):
    monkeypatch.setattr(
        "bank_audit.loophole.adapters.fetch_decorator.fetch_and_parse",
        lambda *a, **k: None,
    )
    assert web_fetch("http://bad.url") is None


@pytest.mark.asyncio
async def test_extract_loopholes_returns_empty_on_empty_text():
    from bank_audit.loophole.chat.tools_nanobot import extract_loopholes

    assert await extract_loopholes("") == []
