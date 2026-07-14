from bank_audit.loophole.chat.llm_adapters import (
    GeminiAdapter,
    OpenAIAdapter,
    QwenAdapter,
    create_adapter,
)


def test_factory_selects_qwen_by_name():
    adapter = create_adapter("qwen3.6")
    assert isinstance(adapter, QwenAdapter)
    assert adapter.provider_name == "dashscope"
    assert adapter.wire_model_name("qwen3.6") == "qwen3.6"
    assert adapter.wire_model_name("dashscope/qwen3.6") == "qwen3.6"


def test_factory_selects_qwen_by_route_prefix():
    adapter = create_adapter("dashscope/qwen3.6-72b")
    assert isinstance(adapter, QwenAdapter)


def test_factory_selects_gemini_by_name():
    adapter = create_adapter("gemini-1.5-pro")
    assert isinstance(adapter, GeminiAdapter)
    assert adapter.provider_name == "gemini"
    assert adapter.wire_model_name("gemini-1.5-pro") == "gemini-1.5-pro"


def test_factory_selects_gemini_by_route_prefix():
    adapter = create_adapter("gemini/gemma-2-9b")
    assert isinstance(adapter, GeminiAdapter)


def test_factory_falls_back_to_openai():
    adapter = create_adapter("gpt-4o")
    assert isinstance(adapter, OpenAIAdapter)
    assert adapter.provider_name == "openai"


def test_factory_falls_back_for_empty_model():
    adapter = create_adapter("")
    assert isinstance(adapter, OpenAIAdapter)


def test_qwen_prepare_request_has_dashscope_base():
    adapter = QwenAdapter()
    cfg = adapter.prepare_request(model="qwen3.6", temperature=0.3)
    assert cfg["apiBase"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert "apiKey" in cfg


def test_gemini_prepare_request_has_gemini_base():
    adapter = GeminiAdapter()
    cfg = adapter.prepare_request(model="gemini-1.5-pro", temperature=0.3)
    assert cfg["apiBase"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert "apiKey" in cfg


def test_openai_prepare_request_uses_env_base(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    adapter = OpenAIAdapter()
    cfg = adapter.prepare_request(model="gpt-4o", temperature=0.3)
    assert cfg["apiBase"] == "http://localhost:9999/v1"
    assert cfg["apiKey"] == "test-key"
