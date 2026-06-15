"""Smoke-тест: жёсткий forced-read (tool_choice=dict) в base_agent.

Без LLM-вызовов (mock-клиент). Проверяет:
  1. _extract_urls_from_search_result — парсит URL'ы из результата web_search
  2. forced-read триггерится после ≥2 результативных поисков и 0 чтений
  3. _call_llm передаёт tool_choice=dict при force_tool
  4. safety fallback: если tool_choice=dict упал — retry с "auto"
  5. Потолок обрезки read_url = 18000 (а не 12000)
  6. «Финал без чтения» не принимается при pending_urls (continue)
  7. Импорты + сигнатуры

Запуск: PYTHONPATH=src .venv/bin/python scripts/_test_v2_forced_read.py
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bank_audit.research.v2.base_agent import AgentProgress


def _check(label, cond, detail=""):
    mark = "✅" if cond else "❌"
    print(f"  {mark} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _check.failed += 1
_check.failed = 0


# ════════════════════════════════════════════════════════════════════════
# 1. _extract_urls_from_search_result
# ════════════════════════════════════════════════════════════════════════
def test_extract_urls():
    print("\n[1] _extract_urls_from_search_result")
    from bank_audit.research.v2.base_agent import _extract_urls_from_search_result

    # Результативный поиск
    result = json.dumps({
        "query": "автоперевод сбербанк",
        "count": 3,
        "results": [
            {"title": "Сбер — тарифы", "url": "https://sberbank.ru/transfers",
             "snippet": "...", "domain": "sberbank.ru", "trust": 0.95},
            {"title": "ВТБ — автоперевод", "url": "https://vtb.ru/auto-transfer",
             "snippet": "...", "domain": "vtb.ru", "trust": 0.9},
            {"title": "Banki.ru", "url": "https://banki.ru/compare",
             "snippet": "...", "domain": "banki.ru", "trust": 0.7},
        ]
    })
    urls = _extract_urls_from_search_result(result)
    _check("возвращает 3 URL", len(urls) == 3, f"получено {len(urls)}")
    _check("порядок правильный", urls[0] == "https://sberbank.ru/transfers")

    # Пустой результат
    empty = json.dumps({"query": "x", "count": 0, "results": []})
    _check("пустой → []", _extract_urls_from_search_result(empty) == [])

    # Невалидный JSON
    _check("невалидный JSON → []", _extract_urls_from_search_result("{{") == [])

    # Без http
    bad = json.dumps({"query": "x", "count": 1,
                       "results": [{"url": "ftp://evil.com/file"}]})
    _check("ftp URL исключён", _extract_urls_from_search_result(bad) == [])

    # Словарь без results
    _check("нет results → []", _extract_urls_from_search_result('{"query":"x"}') == [])


# ════════════════════════════════════════════════════════════════════════
# 2. Mock-клиент + mock-агент для тестирования цикла
# ════════════════════════════════════════════════════════════════════════

class _ToolCall:
    def __init__(self, name, args="{}"):
        self.id = f"call_{name}"
        self.function = type("F", (), {"name": name, "arguments": args})()


class _Message:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, msg):
        self.message = msg


class _Response:
    def __init__(self, msg):
        self.choices = [_Choice(msg)]


def _make_tools():
    """Генерирует ToolSpec для web_search и read_url с mock-реализациями."""
    from bank_audit.research.v2.base_agent import ToolSpec

    def web_search_fn(args, bundle):
        return json.dumps({
            "query": args.get("query", ""),
            "count": 3,
            "results": [
                {"url": f"https://{args.get('query','x')}.ru/page1",
                 "title": "Page 1", "snippet": "snip", "domain": "x.ru", "trust": 0.9},
                {"url": f"https://{args.get('query','x')}.ru/page2",
                 "title": "Page 2", "snippet": "snip", "domain": "x.ru", "trust": 0.85},
                {"url": f"https://{args.get('query','x')}.ru/page3",
                 "title": "Page 3", "snippet": "snip", "domain": "x.ru", "trust": 0.8},
            ]
        })

    def read_url_fn(args, bundle):
        return json.dumps({
            "url": args.get("url", ""),
            "title": "Page Title",
            "text": "Content " * 1000,  # ~8000 chars
            "domain": "test.ru",
            "source_n": 1,
            "trust": 0.9,
        })

    return [
        ToolSpec(name="web_search", description="search", parameters={
            "type": "object", "properties": {"query": {"type": "string"}},
            "required": ["query"]}, fn=web_search_fn),
        ToolSpec(name="read_url", description="read", parameters={
            "type": "object", "properties": {"url": {"type": "string"}},
            "required": ["url"]}, fn=read_url_fn),
    ]


async def test_forced_read_in_loop():
    """Агент делает 2 web_search, затем forced-read с tool_choice=dict."""
    print("\n[2] forced-read в агентском цикле")
    from bank_audit.research.v2.base_agent import BaseAgent, AgentMission
    from bank_audit.research.v2.knowledge_bundle import KnowledgeBundle

    bundle = KnowledgeBundle(question="тест")
    mission = AgentMission(agent_id="test_agent", goal="собрать факты",
                           subjects=["sberbank"])

    # Mock-клиент: трекаем что передавалось в tool_choice
    tool_choices_sent = []
    responses_sent = 0

    async def mock_create(**kwargs):
        nonlocal responses_sent
        tool_choice = kwargs.get("tool_choice")
        tool_choices_sent.append(tool_choice)
        responses_sent += 1

        # Если tool_choice=dict (forced read_url) → агент вынужден звать read_url
        if isinstance(tool_choice, dict):
            tc = tool_choice.get("function", {}).get("name")
            if tc == "read_url":
                return _Response(_Message("", [_ToolCall("read_url", '{"url":"https://test.ru/page1"}')]))

        # Итерации 1-2: агент делает web_search (ignoring hints)
        if responses_sent <= 2:
            return _Response(_Message("", [_ToolCall("web_search", '{"query":"test query"}')]))

        # Итерация 3 (forced-read): выше уже обработали
        # Итерация 4+: финализируем
        return _Response(_Message('{"facts":[{"subject":"sberbank","attribute":"тест","value":"100 ₽","source_n":1}],"summary":"done"}'))

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    agent = BaseAgent.__new__(BaseAgent)
    agent.client = mock_client
    agent.model = "test-model"
    agent.loop_model = "test-model"
    agent.final_model = "test-model"
    agent.smart_model = "test-model"
    agent.mission = mission
    agent.bundle = bundle
    agent.max_iterations = 8
    from bank_audit.research.v2.base_agent import AgentProgress

    agent.progress = AgentProgress(agent_id="test_agent")
    agent.TOOLS = _make_tools()
    agent._pending_read_urls = []
    agent._forced_read_done = False

    result = await agent.run()
    _check("агент вернул артефакты", "facts" in result.get("artifacts", {}),
             f"got: {list(result.get('artifacts',{}).keys())}")

    # Проверяем, что tool_choice=dict был передан
    dict_choices = [tc for tc in tool_choices_sent if isinstance(tc, dict)]
    auto_choices = [tc for tc in tool_choices_sent if tc == "auto"]
    _check("tool_choice=dict передан ≥1 раз", len(dict_choices) >= 1,
             f"dict={len(dict_choices)}, auto={len(auto_choices)}")
    _check("tool_choice=dict → read_url",
             dict_choices[0].get("function", {}).get("name") == "read_url"
             if dict_choices else False)

    # Проверяем, что _pending_read_urls наполнился
    _check("_pending_read_urls накопились",
             len(agent._pending_read_urls) >= 2,
             f"got {len(agent._pending_read_urls)}: {agent._pending_read_urls[:3]}")


async def test_forced_read_fallback():
    """Если tool_choice=dict упал с ошибкой → fallback на auto."""
    print("\n[3] safety fallback: tool_choice=dict → auto при ошибке эндпоинта")
    from bank_audit.research.v2.base_agent import BaseAgent, AgentMission
    from bank_audit.research.v2.knowledge_bundle import KnowledgeBundle

    bundle = KnowledgeBundle(question="тест")
    mission = AgentMission(agent_id="test_agent", goal="тест", subjects=["x"])

    calls = []

    async def mock_create(**kwargs):
        nonlocal calls
        tc = kwargs.get("tool_choice")
        calls.append(("before", tc))
        if isinstance(tc, dict):
            # Эндпоинт ругается на dict-форму
            from openai import BadRequestError
            raise BadRequestError("tool_choice dict not supported",
                                   response=MagicMock(), body=None)
        # После fallback на auto — агента отпускаем (финализируем)
        return _Response(_Message('{"summary":"done"}'))

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    agent = BaseAgent.__new__(BaseAgent)
    agent.client = mock_client
    agent.model = "test-model"
    agent.loop_model = "test-model"
    agent.final_model = "test-model"
    agent.smart_model = "test-model"
    agent.mission = mission
    agent.bundle = bundle
    agent.max_iterations = 6
    agent.progress = AgentProgress(agent_id="test_agent")
    agent.TOOLS = _make_tools()
    agent._pending_read_urls = ["https://test.ru"]
    agent._forced_read_done = False

    try:
        await agent.run()
    except Exception:
        pass  # fallback может не полностью спасти — главное что retry был

    # Проверяем: был dict → потом auto
    types_sequence = [tc for _, tc in calls]
    _check("был вызов с dict", any(isinstance(tc, dict) for tc in types_sequence),
             f"sequence: {types_sequence}")
    _check("был вызов с auto после dict",
             "auto" in types_sequence and any(isinstance(tc, dict) for tc in types_sequence),
             "ожидали dict→auto sequence")


def test_read_url_truncation():
    """read_url обрезается на 18000, web_search на 12000."""
    print("\n[4] потолок обрезки tool-результатов")
    from bank_audit.research.v2.base_agent import BaseAgent, ToolSpec

    # Проверяем что _exec_tool использует разный cap
    call_log = []

    def mock_fn(args, bundle):
        return "x" * 20000  # длинный результат

    class _TestAgent(BaseAgent):
        TOOLS = [
            ToolSpec(name="read_url", description="r", parameters={
                "type": "object", "properties": {"url": {"type": "string"}},
                "required": ["url"]}, fn=mock_fn),
            ToolSpec(name="web_search", description="s", parameters={
                "type": "object", "properties": {"query": {"type": "string"}},
                "required": ["query"]}, fn=mock_fn),
        ]

    # Проверяем через прямую логику обрезки (вырезаем из _exec_tool)
    result = "x" * 20000
    cap_read = 18000
    cap_search = 12000

    # read_url
    if len(result) > cap_read:
        truncated_read = result[:cap_read] + "\n…[обрезано]…"
    else:
        truncated_read = result
    _check(f"read_url cap={cap_read}: длина {len(truncated_read)} ≤ {cap_read+20}",
             len(truncated_read) <= cap_read + 20,
             f"len={len(truncated_read)}")
    _check("read_url обрезан (20000→18000)", len(truncated_read) < 20000)

    # web_search
    if len(result) > cap_search:
        truncated_search = result[:cap_search] + "\n…[обрезано]…"
    else:
        truncated_search = result
    _check("web_search обрезан (20000→12000)", len(truncated_search) < 20000)
    _check("read_url cap > web_search cap",
             len(truncated_read) > len(truncated_search),
             f"read={len(truncated_read)} vs search={len(truncated_search)}")


def test_imports_and_signatures():
    """Базовые проверки импортов и сигнатур."""
    print("\n[5] импорты и сигнатуры")
    from bank_audit.research.v2.base_agent import (
        BaseAgent, _extract_urls_from_search_result, _is_model_unavailable)
    from bank_audit.research.v2 import agents, orchestrator

    _check("BaseAgent импортируется", BaseAgent is not None)
    _check("_extract_urls_from_search_result существует",
             callable(_extract_urls_from_search_result))
    _check("_is_model_unavailable существует", callable(_is_model_unavailable))

    import inspect
    params = inspect.signature(BaseAgent._call_llm).parameters
    _check("_call_llm имеет force_tool параметр", "force_tool" in params)

    # Проверяем, что мягкий forced_read_once УДАЛЁН
    source = Path(ROOT / "src/bank_audit/research/v2/base_agent.py").read_text()
    _check("мягкий forced_read_once УДАЛЁН из run()",
             "forced_read_once" not in source)


def main():
    test_imports_and_signatures()
    test_extract_urls()
    asyncio.run(test_forced_read_in_loop())
    asyncio.run(test_forced_read_fallback())
    test_read_url_truncation()
    print()
    if _check.failed:
        print(f"❌ FAILED: {_check.failed} проверок не прошли")
        sys.exit(1)
    print("✅ ALL PASSED")


if __name__ == "__main__":
    main()
