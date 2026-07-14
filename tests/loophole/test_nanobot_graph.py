import pytest
from bank_audit.loophole.chat import graph as graph_mod
from bank_audit.loophole.chat.graph import run_chat, stream_chat
from bank_audit.loophole.chat.state import ChatState
from nanobot.sdk.types import (
    STREAM_EVENT_RUN_COMPLETED,
    STREAM_EVENT_TEXT_COMPLETED,
    STREAM_EVENT_TEXT_DELTA,
    StreamEvent,
)


class _FakeNanobot:
    """Фейковый nanobot для юнит-тестов graph.py."""

    def __init__(self, stream_events: list[StreamEvent] | None = None, run_content: str = ""):
        self._stream_events = list(stream_events or [])
        self._run_content = run_content
        self.closed = False
        self.config_path = "/tmp/fake_nanobot.json"

    async def run(self, *args, **kwargs):
        class _Result:
            content = self._run_content

        return _Result()

    async def stream(self, *args, **kwargs):
        for ev in self._stream_events:
            yield ev

    async def aclose(self):
        self.closed = True


def _fake_create_nanobot(model=None, **kwargs):
    return _FakeNanobot(run_content="Результат run"), "/tmp/fake_nanobot.json"


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
async def test_run_chat_complete_does_not_crash(monkeypatch):
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    async def fake_gen(question, history=None):
        return {"complete": True, "questions": []}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    state: ChatState = {"query": "сколько записей в базе", "workspace_id": 1, "user_id": "u1"}
    out = await run_chat(state)
    assert out["phase"] == "done"
    assert "answer" in out


@pytest.mark.asyncio
async def test_run_chat_no_reclarify_when_answers_present(monkeypatch):
    """Если пользователь уже ответил на clarify-вопросы, воронка не должна
    запускаться повторно — иначе агент зацикливается на вопросах."""
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    calls = []

    async def fake_gen(question, history=None):
        calls.append((question, history))
        return {"complete": False, "questions": [{"id": "q1", "question": "Какой банк?"}]}

    async def fake_rewrite(question, answers):
        return f"{question} (банк: Сбербанк)"

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", fake_rewrite)

    state: ChatState = {
        "query": "найди лазейки",
        "workspace_id": 1,
        "user_id": "u1",
        "clarify_answers": [{"question": "Какой банк?", "selected": ["Сбербанк"], "other": ""}],
    }
    out = await run_chat(state)
    assert not calls, "generate_clarifications не должен вызываться, если уже есть clarify_answers"
    assert out["phase"] == "done" or out.get("answer") is not None or "error" in out


@pytest.mark.asyncio
async def test_stream_chat_no_reclarify_when_answers_present(monkeypatch):
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    calls = []

    async def fake_gen(question, history=None):
        calls.append((question, history))
        return {"complete": False, "questions": [{"id": "q1", "question": "Какой банк?"}]}

    async def fake_rewrite(question, answers):
        return f"{question} (банк: Сбербанк)"

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", fake_rewrite)

    state: ChatState = {
        "query": "найди лазейки",
        "workspace_id": 1,
        "user_id": "u1",
        "clarify_answers": [{"question": "Какой банк?", "selected": ["Сбербанк"], "other": ""}],
    }
    events = []
    async for ev in stream_chat(state):
        events.append(ev)
    assert not calls, "stream_chat не должен вызывать generate_clarifications при наличии ответов"
    assert not any(e["event"] == "phase" and e["data"].get("phase") == "await_clarify" for e in events)


@pytest.mark.asyncio
async def test_stream_chat_emits_run_completed_as_token(monkeypatch):
    """run.completed должен транслироваться в token-событие с ответом."""
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    async def fake_clarify(q, history=None):
        return {"complete": True, "questions": []}

    async def fake_rewrite(q, a):
        return q

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_clarify)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", fake_rewrite)
    monkeypatch.setattr(graph_mod, "create_nanobot", lambda **kwargs: (
        _FakeNanobot(stream_events=[StreamEvent(type=STREAM_EVENT_RUN_COMPLETED, content="Русский ответ")]),
        "/tmp/fake_nanobot.json",
    ))

    state: ChatState = {"query": "привет", "workspace_id": 1, "user_id": "u1", "clarify_answers": []}
    events = [ev async for ev in stream_chat(state)]
    tokens = [e for e in events if e["event"] == "token"]
    assert any("Русский ответ" in e["data"] for e in tokens), tokens


@pytest.mark.asyncio
async def test_stream_chat_emits_text_completed_and_run_completed(monkeypatch):
    """text.completed с непустым текстом тоже должен идти как token."""
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    async def fake_clarify(q, history=None):
        return {"complete": True, "questions": []}

    async def fake_rewrite(q, a):
        return q

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_clarify)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", fake_rewrite)
    monkeypatch.setattr(graph_mod, "create_nanobot", lambda **kwargs: (
        _FakeNanobot(
            stream_events=[
                StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta="Часть 1. "),
                StreamEvent(type=STREAM_EVENT_TEXT_COMPLETED, content="Часть 1. "),
                StreamEvent(type=STREAM_EVENT_RUN_COMPLETED, content="Итоговый ответ"),
            ]
        ),
        "/tmp/fake_nanobot.json",
    ))

    state: ChatState = {"query": "привет", "workspace_id": 1, "user_id": "u1", "clarify_answers": []}
    events = [ev async for ev in stream_chat(state)]
    tokens = [e for e in events if e["event"] == "token"]
    assert tokens[-1]["data"] == "Итоговый ответ", tokens


@pytest.mark.asyncio
async def test_run_chat_returns_answer_from_nanobot(monkeypatch):
    """run_chat должен возвращать answer, полученный от nanobot.run()."""
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    async def fake_clarify(q, history=None):
        return {"complete": True, "questions": []}

    async def fake_rewrite(q, a):
        return q

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_clarify)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", fake_rewrite)
    monkeypatch.setattr(graph_mod, "create_nanobot", lambda **kwargs: (
        _FakeNanobot(run_content="Ответ от агента"),
        "/tmp/fake_nanobot.json",
    ))

    state: ChatState = {"query": "привет", "workspace_id": 1, "user_id": "u1", "clarify_answers": []}
    out = await run_chat(state)
    assert out["answer"] == "Ответ от агента"


def test_loophole_init_sets_utf8_stdio(monkeypatch):
    """Импорт модуля loophole должен переводить stdout/stderr на utf-8."""
    import sys
    import io

    # Симулируем cp1251-кодировку, чтобы проверить, что патч сработает.
    class _FakeStream:
        encoding = "cp1251"
        buffer = io.BytesIO()

    monkeypatch.setattr(sys, "stdout", _FakeStream())
    monkeypatch.setattr(sys, "stderr", _FakeStream())

    # Переимпортируем модуль, чтобы патч применился заново.
    import importlib
    import bank_audit.loophole

    importlib.reload(bank_audit.loophole)

    assert sys.stdout.encoding == "utf-8"
    assert sys.stderr.encoding == "utf-8"
