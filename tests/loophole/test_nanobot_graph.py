import pytest

from bank_audit.loophole.chat.graph import run_chat, stream_chat
from bank_audit.loophole.chat.state import ChatState


@pytest.mark.asyncio
async def test_run_chat_await_clarify(monkeypatch):
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    async def fake_gen(question, history=None):
        return {"complete": False, "questions": [{"id": "q1", "question": "Какой банк?"}]}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    state: ChatState = {"query": "найди лазейки", "workspace_id": 1, "user_id": "u1"}
    out = await run_chat(state)
    assert out["phase"] == "await_clarify"
    assert len(out["clarify_questions"]) == 1


@pytest.mark.asyncio
async def test_run_chat_complete_does_not_crash(monkeypatch, session):
    from bank_audit.loophole.agent import AgentResult
    from bank_audit.loophole.chat import clarify as clarify_mod
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    _create_research_schema(session)

    async def fake_gen(question, history=None):
        return {"complete": True, "questions": []}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    async def fake_run(state, *, llm=None, session=None):
        return AgentResult(answer="Исследование выполнено", run_id=state["run_id"])

    monkeypatch.setattr("bank_audit.loophole.chat.graph._run_nanobot", fake_run)

    state: ChatState = {"query": "сколько записей в базе", "workspace_id": 1, "user_id": "u1"}
    out = await run_chat(state, session=session)
    assert out["phase"] == "done"
    assert "answer" in out


@pytest.mark.asyncio
async def test_stream_chat_await_clarify(monkeypatch):
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    async def fake_gen(question, history=None):
        return {"complete": False, "questions": [{"id": "q1", "question": "Какой банк?"}]}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    state: ChatState = {"query": "найди лазейки", "workspace_id": 1, "user_id": "u1"}
    events = []
    async for ev in stream_chat(state):
        events.append(ev)
    assert any(
        e["event"] == "phase" and e["data"].get("phase") == "await_clarify" for e in events
    )


@pytest.mark.asyncio
async def test_stream_chat_saves_confirmed_findings_and_auto_imports_to_catalog(
    monkeypatch, session
):
    """Мутация: managed run сохраняет исследование и сразу переносит
    подтверждённые находки в общий каталог со статусом preliminary."""
    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context

        async def stream(self, _prompt, *, hook):
            self.context.fetched_sources["https://example.ru/source"] = {
                "url": "https://example.ru/source",
                "title": "Источник",
                "extracted_text": "Текст источника",
                "published_at": None,
            }
            self.context.pending_records.append(
                {
                    "title": "Обход комиссии",
                    "url": "https://example.ru/source",
                    "snippet": "Подтверждающая цитата",
                    "bank_slug": "sberbank",
                    "raw_text": "Текст источника",
                    "is_loophole": True,
                }
            )
            hook.final_answer = "Готово"
            if False:
                yield None

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, **_kwargs):
            return FakeAgent(context)

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    state: ChatState = {
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }
    _events = [event async for event in stream_chat(state, session=session)]

    record = session.execute(
        text("SELECT status, is_loophole FROM loophole_record")
    ).mappings().one()
    assert record["status"] == "preliminary"
    assert record["is_loophole"] in (True, 1)
    source = session.execute(
        text(
            "SELECT source.url, source.extracted_text "
            "FROM loophole_research_source AS source "
            "JOIN loophole_research AS research ON research.research_id = source.research_id "
            "WHERE research.workspace_id = 1"
        )
    ).mappings().one_or_none()
    assert source == {
        "url": "https://example.ru/source",
        "extracted_text": "Текст источника",
    }


@pytest.mark.asyncio
async def test_stream_partial_run_does_not_persist_on_fatal_error(monkeypatch, session):
    """Фатальный сбой (провайдер/протокол): persist запрещён, но с warning-логом
    о числе отброшенных находок и кодах ошибок."""
    from sqlalchemy import text
    from structlog.testing import capture_logs

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context

        async def stream(self, _prompt, *, hook):
            self.context.fetched_sources["https://example.ru/source"] = {
                "url": "https://example.ru/source",
                "title": "Источник",
                "extracted_text": "Прочитанный текст",
                "published_at": "2026-08-01T00:00:00+03:00",
            }
            self.context.pending_records.append({
                "title": "Подозрение",
                "url": "https://example.ru/source",
                "snippet": "Цитата",
                "raw_text": "Прочитанный текст",
                "is_loophole": True,
            })
            hook.stop_reason = "error"
            if False:
                yield None

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, **_kwargs):
            return FakeAgent(context)

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    with capture_logs() as logs:
        events = [event async for event in graph.stream_chat({
            "query": "Найди лазейки",
            "workspace_id": 1,
            "user_id": "analyst",
            "clarification_verified": True,
        }, session=session)]

    assert not any(event["event"] == "records" for event in events)
    assert session.execute(text("SELECT count(*) FROM loophole_research")).scalar_one() == 0
    warnings = [entry for entry in logs if entry.get("event") == "loophole_persist_skipped_fatal"]
    assert len(warnings) == 1
    assert warnings[0]["pending_count"] == 1
    assert "agent_error" in warnings[0]["errors"]


def _partial_agent_with_finding(context, hook, *, stop_reason=None, fail_tool=False):
    """Общий сценарий fake-агента: валидная находка + нефатальное завершение."""
    context.fetched_sources["https://example.ru/source"] = {
        "url": "https://example.ru/source",
        "title": "Источник",
        "extracted_text": "Прочитанный текст про обход комиссии через СБП.",
        "published_at": None,
    }
    context.pending_records.append({
        "title": "Обход комиссии",
        "url": "https://example.ru/source",
        "snippet": "обход комиссии",
        "evidence_quote": "обход комиссии",
        "raw_text": "Прочитанный текст про обход комиссии через СБП.",
        "bank_slug": "sberbank",
        "is_loophole": True,
    })
    if fail_tool:
        hook.record_stream_event("tool.failed", name="audit_web_fetch", error="timeout")
    if stop_reason:
        hook.stop_reason = stop_reason


@pytest.mark.asyncio
async def test_stream_partial_run_persists_on_skill_failed(monkeypatch, session):
    """Флаки одного web_fetch (skill_failed) не обесценивает валидные находки:
    persist выполняется через строгую валидацию eligible_findings."""
    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context

        async def stream(self, _prompt, *, hook):
            _partial_agent_with_finding(self.context, hook, fail_tool=True)
            hook.final_answer = "Черновой ответ"
            if False:
                yield None

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, **_kwargs):
            return FakeAgent(context)

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    events = [event async for event in graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]

    assert any(event["event"] == "records" for event in events)
    assert session.execute(text("SELECT count(*) FROM loophole_research")).scalar_one() == 1
    record = session.execute(
        text("SELECT status, is_loophole FROM loophole_record")
    ).mappings().one()
    assert record["status"] == "preliminary"
    assert record["is_loophole"] in (True, 1)


@pytest.mark.asyncio
async def test_stream_partial_run_persists_on_max_iterations(monkeypatch, session):
    """Лимит итераций (max_iterations) — нефатальное завершение: валидные
    находки сохраняются, partial-пояснение в ответе сохраняется."""
    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context

        async def stream(self, _prompt, *, hook):
            _partial_agent_with_finding(self.context, hook, stop_reason="max_iterations")
            hook.final_answer = "Черновой ответ"
            if False:
                yield None

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, **_kwargs):
            return FakeAgent(context)

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    events = [event async for event in graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]

    assert any(event["event"] == "records" for event in events)
    assert session.execute(text("SELECT count(*) FROM loophole_research")).scalar_one() == 1
    assert any(
        event["event"] == "phase"
        and event["data"].get("phase") == "answer"
        and event["data"].get("partial") is True
        for event in events
    )


@pytest.mark.asyncio
async def test_stream_chat_runs_web_fallback_when_no_findings_and_no_search(
    monkeypatch, session
):
    """Пустой результат без веб-поиска: серверный fallback запускается до persist,
    его находки сохраняются, прогресс виден в SSE."""
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context
            self._activity_events = asyncio.Queue()
            self.fallback_called = False

        async def stream(self, _prompt, *, hook):
            hook.final_answer = "По данным базы ничего не найдено."
            if False:
                yield None

        def web_fallback_needed(self):
            return True

        async def run_web_fallback(self):
            self.fallback_called = True
            _partial_agent_with_finding(self.context, hook=None)
            self._activity_events.put_nowait(SimpleNamespace(
                type="audit.tool",
                metadata={"name": "audit_web_search", "status": "completed"},
            ))
            return True

        async def aclose(self):
            return None

    agents = []

    class FakeFactory:
        def create(self, context, **_kwargs):
            agent = FakeAgent(context)
            agents.append(agent)
            return agent

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    events = [event async for event in graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]

    assert agents[0].fallback_called is True
    assert any(
        event["event"] == "tool_result"
        and event["data"].get("name") == "audit_web_search"
        for event in events
    )
    assert any(event["event"] == "records" for event in events)
    assert session.execute(text("SELECT count(*) FROM loophole_research")).scalar_one() == 1


@pytest.mark.asyncio
async def test_stream_chat_skips_fallback_when_findings_exist(monkeypatch, session):
    """Валидные находки уже есть — fallback не нужен."""
    import asyncio

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context
            self._activity_events = asyncio.Queue()
            self.fallback_called = False

        async def stream(self, _prompt, *, hook):
            _partial_agent_with_finding(self.context, hook)
            hook.final_answer = "Готово"
            if False:
                yield None

        def web_fallback_needed(self):
            return True

        async def run_web_fallback(self):
            self.fallback_called = True
            return True

        async def aclose(self):
            return None

    agents = []

    class FakeFactory:
        def create(self, context, **_kwargs):
            agent = FakeAgent(context)
            agents.append(agent)
            return agent

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    events = [event async for event in graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]

    assert agents[0].fallback_called is False
    assert any(event["event"] == "records" for event in events)


@pytest.mark.asyncio
async def test_stream_chat_skips_fallback_on_fatal_error(monkeypatch, session):
    """Фатальная ошибка — fallback не запускается."""
    import asyncio

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context
            self._activity_events = asyncio.Queue()
            self.fallback_called = False

        async def stream(self, _prompt, *, hook):
            hook.stop_reason = "error"
            if False:
                yield None

        def web_fallback_needed(self):
            return True

        async def run_web_fallback(self):
            self.fallback_called = True
            return True

        async def aclose(self):
            return None

    agents = []

    class FakeFactory:
        def create(self, context, **_kwargs):
            agent = FakeAgent(context)
            agents.append(agent)
            return agent

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    _events = [event async for event in graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]

    assert agents[0].fallback_called is False


@pytest.mark.asyncio
async def test_stream_chat_skips_fallback_on_cancellation(monkeypatch, session):
    """Отмена пользователем (закрытие SSE-генератора) — fallback не запускается."""
    import asyncio
    from types import SimpleNamespace

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context
            self._activity_events = asyncio.Queue()
            self.fallback_called = False

        async def stream(self, _prompt, *, hook):
            yield SimpleNamespace(type="run.progress", metadata={
                "stage": "waiting_model", "elapsed_seconds": 0,
            })
            # Ожидание отмены: aclose генератора бросит GeneratorExit в этом await.
            await asyncio.Event().wait()

        def web_fallback_needed(self):
            return True

        async def run_web_fallback(self):
            self.fallback_called = True
            return True

        async def aclose(self):
            return None

    agents = []

    class FakeFactory:
        def create(self, context, **_kwargs):
            agent = FakeAgent(context)
            agents.append(agent)
            return agent

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    agen = graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)
    seen_progress = False
    async for event in agen:
        if event["event"] == "phase" and event["data"].get("stage") == "waiting_model":
            seen_progress = True
            break
    await agen.aclose()

    assert seen_progress is True
    assert agents[0].fallback_called is False


# ── ManagedAgent.run_web_fallback (детерминированный серверный fallback) ─────


def _fallback_agent(*, timeout_seconds: float = 600.0):
    """Собирает ManagedAgent без nanobot-бота для изолированной проверки fallback."""
    from bank_audit.loophole.agent import AgentRunContext, ManagedAgent
    from bank_audit.loophole.run_budget import ResearchBudget

    budget = ResearchBudget(timeout_seconds=timeout_seconds)
    context = AgentRunContext(
        user_id="analyst",
        workspace_id=1,
        query="лазейки по кредитным картам",
        run_id="run-fallback",
        budget=budget,
    )
    return ManagedAgent(context, bot=None, config_path="")


def _mock_web(monkeypatch, searches: list[str]):
    """Подменяет сеть и LLM-извлечение в tools_nanobot (без реальных вызовов)."""
    from bank_audit.loophole.chat import tools_nanobot

    def fake_web_search(query, *, max_results=12, _impl=None):
        searches.append(query)
        return [{
            "title": "Форум",
            "url": f"https://example.ru/thread-{len(searches)}",
            "snippet": "обсуждение схемы",
            "domain": "example.ru",
        }]

    def fake_web_fetch(url, *, _impl=None):
        return {
            "url": url,
            "final_url": url,
            "status": 200,
            "title": "Форум",
            "excerpt": "Текст про обход комиссии через СБП.",
            "via": "http",
            "published_at": None,
            "estimated_published_at": None,
        }

    async def fake_extract(text, *, llm=None):
        return [{
            "title": "Обход комиссии",
            "description": "Вывод через СБП как покупка",
            "category": "fees",
            "severity": "medium",
            "evidence_quote": "обход комиссии",
            "is_loophole": True,
        }]

    monkeypatch.setattr(tools_nanobot, "web_search", fake_web_search)
    monkeypatch.setattr(tools_nanobot, "web_fetch", fake_web_fetch)
    monkeypatch.setattr(tools_nanobot, "extract_loopholes", fake_extract)


@pytest.mark.asyncio
async def test_run_web_fallback_searches_reads_and_extracts(monkeypatch):
    """Пустой результат без веб-поиска: fallback ищет по ≥2 независимым кластерам,
    читает страницы и извлекает кандидатов в pending_records."""
    from bank_audit.loophole.agent import eligible_findings

    searches: list[str] = []
    _mock_web(monkeypatch, searches)
    agent = _fallback_agent()

    ran = await agent.run_web_fallback()

    assert ran is True
    assert len(searches) >= 2
    assert all(query.startswith("лазейки по кредитным картам") for query in searches)
    assert len(set(searches)) == len(searches), "кластеры должны быть независимыми"
    assert agent._budget.search_results, "выдача должна попасть в бюджет запуска"
    assert agent.context.pending_records, "извлечение должно дать кандидатов"
    assert eligible_findings(agent.context), "кандидаты должны пройти валидацию"


@pytest.mark.asyncio
async def test_run_web_fallback_skipped_when_findings_exist(monkeypatch):
    """Валидные находки уже есть — fallback не запускается."""
    searches: list[str] = []
    _mock_web(monkeypatch, searches)
    agent = _fallback_agent()
    agent.context.fetched_sources["https://example.ru/source"] = {
        "url": "https://example.ru/source",
        "title": "Источник",
        "extracted_text": "Прочитанный текст про обход комиссии через СБП.",
        "published_at": None,
    }
    agent.context.pending_records.append({
        "title": "Обход комиссии",
        "url": "https://example.ru/source",
        "snippet": "обход комиссии",
        "evidence_quote": "обход комиссии",
        "is_loophole": True,
    })

    ran = await agent.run_web_fallback()

    assert ran is False
    assert searches == []


@pytest.mark.asyncio
async def test_run_web_fallback_skipped_when_search_already_used(monkeypatch):
    """Веб-поиск уже выполнялся агентом — fallback не запускается."""
    searches: list[str] = []
    _mock_web(monkeypatch, searches)
    agent = _fallback_agent()
    agent._budget.search_results.append({
        "title": "Форум", "url": "https://example.ru/old", "snippet": "…",
    })

    ran = await agent.run_web_fallback()

    assert ran is False
    assert searches == []


@pytest.mark.asyncio
async def test_run_web_fallback_skipped_when_budget_exhausted(monkeypatch):
    """Остаток бюджета ниже порога — fallback не запускается."""
    searches: list[str] = []
    _mock_web(monkeypatch, searches)
    agent = _fallback_agent(timeout_seconds=5.0)

    ran = await agent.run_web_fallback()

    assert ran is False
    assert searches == []


@pytest.mark.asyncio
async def test_run_web_fallback_survives_search_failure(monkeypatch):
    """Сбой поиска не роняет чат: fallback завершается без исключений и находок."""
    from bank_audit.loophole.chat import tools_nanobot

    def failing_web_search(query, *, max_results=12, _impl=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(tools_nanobot, "web_search", failing_web_search)
    agent = _fallback_agent()

    ran = await agent.run_web_fallback()

    assert ran is True
    assert agent.context.pending_records == []


@pytest.mark.asyncio
async def test_stream_chat_replaces_stale_budget_message_after_fallback(monkeypatch, session):
    """Маркер «не получено кандидатов» + устаревший materials-блок после успешного
    fallback заменяются отчётом о находках целиком, а не дополняются."""
    import asyncio

    from bank_audit.loophole.agent import NO_FINDINGS_BUDGET_MESSAGE
    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    stale_materials = (
        "Прочитанные материалы — сами по себе не подтверждают наличие лазейки:\n"
        "Источник — https://example.ru/old\n"
        "Дата публикации не подтверждена. Извлечение не завершено."
    )

    class FakeAgent:
        def __init__(self, context):
            self.context = context
            self._activity_events = asyncio.Queue()

        async def stream(self, _prompt, *, hook):
            hook.final_answer = NO_FINDINGS_BUDGET_MESSAGE + "\n\n" + stale_materials
            if False:
                yield None

        def web_fallback_needed(self):
            return True

        async def run_web_fallback(self):
            _partial_agent_with_finding(self.context, hook=None)
            return True

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, **_kwargs):
            return FakeAgent(context)

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    events = [event async for event in graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]

    tokens = [event["data"] for event in events if event["event"] == "token"]
    assert tokens, "итоговый ответ должен быть доставлен token-событием"
    final_answer = tokens[-1]
    assert "AI-кандидаты" in final_answer
    assert "Обход комиссии" in final_answer
    assert NO_FINDINGS_BUDGET_MESSAGE not in final_answer
    assert "Извлечение не завершено" not in final_answer


@pytest.mark.asyncio
async def test_stream_chat_delivers_fallback_report_when_streamed(monkeypatch, session):
    """Ответ уже отстримлен клиенту (streamed_any): отчёт о находках fallback
    доставляется отдельным token-событием, hook.final_answer синхронизирован."""
    import asyncio

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context
            self._activity_events = asyncio.Queue()

        async def stream(self, _prompt, *, hook):
            # Модель стримит дельты: streamed_any станет True после flush хвоста.
            hook.stream_delta_for_sse(
                "Черновой ответ: подтверждённых находок пока нет, проверка продолжается. " * 3
            )
            if False:
                yield None

        def web_fallback_needed(self):
            return True

        async def run_web_fallback(self):
            _partial_agent_with_finding(self.context, hook=None)
            return True

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, **_kwargs):
            return FakeAgent(context)

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    events = [event async for event in graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]

    tokens = [event["data"] for event in events if event["event"] == "token"]
    assert any("подтверждённых находок пока нет" in token for token in tokens)
    assert any("AI-кандидаты" in token for token in tokens), (
        "отчёт fallback должен быть доставлен отдельным token-событием"
    )


@pytest.mark.asyncio
async def test_stream_partial_run_persists_on_cleanup_timeout(monkeypatch, session):
    """cleanup_timeout — нефатальный инфраструктурный сбой, симметричный
    skill_failed: валидные находки сохраняются."""
    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context

        async def stream(self, _prompt, *, hook):
            _partial_agent_with_finding(self.context, hook)
            hook.tool_errors.append("cleanup_timeout")
            hook.final_answer = "Черновой ответ"
            if False:
                yield None

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, **_kwargs):
            return FakeAgent(context)

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    events = [event async for event in graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]

    assert any(event["event"] == "records" for event in events)
    assert session.execute(text("SELECT count(*) FROM loophole_research")).scalar_one() == 1


@pytest.mark.asyncio
async def test_stream_chat_runs_fallback_on_partial_errors(monkeypatch, session):
    """Partial-остановка (no_progress) не отменяет fallback: при соблюдении
    условий сервер довыполняет веб-поиск и сохраняет находки."""
    import asyncio

    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context
            self._activity_events = asyncio.Queue()
            self.fallback_called = False

        async def stream(self, _prompt, *, hook):
            hook.tool_errors.append("no_progress")
            hook.final_answer = "До остановки кандидатов не получено."
            if False:
                yield None

        def web_fallback_needed(self):
            return True

        async def run_web_fallback(self):
            self.fallback_called = True
            _partial_agent_with_finding(self.context, hook=None)
            return True

        async def aclose(self):
            return None

    agents = []

    class FakeFactory:
        def create(self, context, **_kwargs):
            agent = FakeAgent(context)
            agents.append(agent)
            return agent

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    events = [event async for event in graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]

    assert agents[0].fallback_called is True
    assert any(event["event"] == "records" for event in events)
    assert session.execute(text("SELECT count(*) FROM loophole_research")).scalar_one() == 1
    assert any(
        event["event"] == "phase"
        and event["data"].get("phase") == "answer"
        and event["data"].get("partial") is True
        for event in events
    )


@pytest.mark.asyncio
async def test_stream_chat_maps_fallback_fetch_activity(monkeypatch, session):
    """Активность audit_web_fetch fallback маппится в tool_call/tool_result SSE."""
    import asyncio
    from types import SimpleNamespace

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context
            self._activity_events = asyncio.Queue()

        async def stream(self, _prompt, *, hook):
            hook.final_answer = "По данным базы ничего не найдено."
            if False:
                yield None

        def web_fallback_needed(self):
            return True

        async def run_web_fallback(self):
            for status in ("running", "completed"):
                self._activity_events.put_nowait(SimpleNamespace(
                    type="audit.tool",
                    metadata={"name": "audit_web_fetch", "status": status},
                ))
            _partial_agent_with_finding(self.context, hook=None)
            return True

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, **_kwargs):
            return FakeAgent(context)

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    events = [event async for event in graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]

    assert any(
        event["event"] == "phase" and event["data"].get("stage") == "web_fallback"
        for event in events
    ), "перед fallback должна эмититься UX-фаза web_fallback"
    assert any(
        event["event"] == "tool_call" and event["data"].get("name") == "audit_web_fetch"
        for event in events
    )
    assert any(
        event["event"] == "tool_result"
        and event["data"].get("name") == "audit_web_fetch"
        and event["data"].get("status") == "completed"
        for event in events
    )


@pytest.mark.asyncio
async def test_stream_chat_cancels_fallback_task_on_client_disconnect(monkeypatch, session):
    """Отмена SSE-генератора в середине fallback: задача fallback отменяется."""
    import asyncio
    from types import SimpleNamespace

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context
            self._activity_events = asyncio.Queue()
            self.fallback_cancelled = False

        async def stream(self, _prompt, *, hook):
            hook.final_answer = "По данным базы ничего не найдено."
            if False:
                yield None

        def web_fallback_needed(self):
            return True

        async def run_web_fallback(self):
            self._activity_events.put_nowait(SimpleNamespace(
                type="audit.tool",
                metadata={"name": "audit_web_search", "status": "running"},
            ))
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.fallback_cancelled = True
                raise
            return True

        async def aclose(self):
            return None

    agents = []

    class FakeFactory:
        def create(self, context, **_kwargs):
            agent = FakeAgent(context)
            agents.append(agent)
            return agent

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    agen = graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)
    seen_fallback_activity = False
    async for event in agen:
        if event["event"] == "tool_call" and event["data"].get("name") == "audit_web_search":
            seen_fallback_activity = True
            break
    await agen.aclose()

    assert seen_fallback_activity is True
    assert agents[0].fallback_cancelled is True


@pytest.mark.asyncio
async def test_run_web_fallback_restores_stop_reason_and_keeps_cancelled(monkeypatch):
    """stop_reason после fallback восстанавливается; budget.cancelled не снимается:
    доступ к tools fallback получает через scoped-флаг своего ToolContext, а
    контексты без флага по-прежнему отсекаются ensure_active."""
    import asyncio as asyncio_mod

    searches: list[str] = []
    _mock_web(monkeypatch, searches)
    agent = _fallback_agent()
    agent._budget.cancelled = True
    agent._budget.stop_reason = "no_progress"

    ran = await agent.run_web_fallback()

    assert ran is True
    assert searches, "scoped-флаг fallback_active пускает вызовы fallback"
    assert agent._budget.cancelled is True, "глобальный cancelled не снимается"
    assert agent._budget.stop_reason == "no_progress", "stop_reason восстановлен"

    from bank_audit.loophole.chat.tools_nanobot import ToolContext, _ensure_tool_active

    plain_ctx = ToolContext("analyst", 1, None, budget=agent._budget)
    try:
        _ensure_tool_active(plain_ctx)
    except asyncio_mod.CancelledError:
        pass
    else:
        raise AssertionError("контекст без fallback_active должен отсекаться cancelled")


def test_web_fallback_clusters_strip_mask_placeholders():
    """Кластеры fallback не содержат плейсхолдеров ПДн-маскировки, а базовый
    запрос обрезается, чтобы в SearXNG не уходили мусорные длинные строки."""
    from bank_audit.loophole.agent import _web_fallback_clusters

    clusters = _web_fallback_clusters("вывод по карте [CARD_1] клиента [FIO_2] без комиссии")
    assert clusters
    assert all("[CARD_1]" not in cluster for cluster in clusters)
    assert all("[FIO_2]" not in cluster for cluster in clusters)
    assert all("карте" in cluster for cluster in clusters)

    long_clusters = _web_fallback_clusters("лазейка " * 50)
    bare = long_clusters[-1]
    assert len(bare) <= 80
