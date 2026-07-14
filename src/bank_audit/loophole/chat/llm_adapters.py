"""Фабрика и адаптеры конфигурации LLM-провайдера для nanobot-агента.

Каждый адаптер готовит параметры вызова API (base URL, ключ, имя провайдера,
wire-имя модели и специфические заголовки/body) на основе имени модели.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any


class LLMConfigAdapter(ABC):
    """Базовый адаптер конфигурации LLM-провайдера для nanobot."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Имя провайдера в конфиге nanobot (openai, dashscope, gemini и т.д.)."""

    @abstractmethod
    def get_api_base(self) -> str:
        """Base URL для API-вызовов."""

    @abstractmethod
    def get_api_key(self) -> str:
        """API-ключ для провайдера."""

    def get_extra_headers(self) -> dict[str, str] | None:
        """Дополнительные заголовки запроса (опционально)."""
        return None

    def get_extra_body(self) -> dict[str, Any] | None:
        """Дополнительное тело запроса (опционально)."""
        return None

    def wire_model_name(self, model_name: str) -> str:
        """Имя модели, которое отправляется на провайдер (без route-префиксов)."""
        return model_name

    def prepare_request(self, *, model: str, temperature: float, max_tokens: int | None = None) -> dict[str, Any]:
        """Готовит полный конфигурационный блок провайдера для nanobot."""
        cfg: dict[str, Any] = {
            "apiBase": self.get_api_base(),
            "apiKey": self.get_api_key(),
        }
        extra_headers = self.get_extra_headers()
        extra_body = self.get_extra_body()
        if extra_headers:
            cfg["extraHeaders"] = extra_headers
        if extra_body:
            cfg["extraBody"] = extra_body
        return cfg

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(provider={self.provider_name})"


class _EnvMixin:
    """Хелпер для чтения env-переменных с очисткой inline-комментариев."""

    @staticmethod
    def _clean_env(value: str | None) -> str:
        if not value:
            return ""
        # .env может содержать inline-комментарий на русском; httpx падает с
        # UnicodeEncodeError при формировании Authorization-заголовка.
        return (value.split("#", 1)[0]).strip()


class OpenAIAdapter(_EnvMixin, LLMConfigAdapter):
    """Адаптер для OpenAI-совместимых endpoint'ов (fallback)."""

    @property
    def provider_name(self) -> str:
        return "openai"

    def get_api_base(self) -> str:
        return self._clean_env(os.getenv("LLM_BASE_URL", ""))

    def get_api_key(self) -> str:
        return self._clean_env(os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")))


class QwenAdapter(_EnvMixin, LLMConfigAdapter):
    """Адаптер для моделей семейства Qwen (через DashScope OpenAI-compatible API).

    Поддерживает имена вида ``qwen3.6``, ``qwen3.6-xxx``, ``dashscope/qwen3.6``.
    """

    @property
    def provider_name(self) -> str:
        return "dashscope"

    def get_api_base(self) -> str:
        return self._clean_env(os.getenv("LLM_BASE_URL", ""))

    def get_api_key(self) -> str:
        return self._clean_env(
            os.getenv("DASHSCOPE_API_KEY") or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        )

    def wire_model_name(self, model_name: str) -> str:
        # DashScope нативно понимает префикс "qwen-", но route-префикс
        # "dashscope/" используется только для выбора адаптера.
        return model_name.split("/", 1)[-1]

    def get_extra_body(self) -> dict[str, Any] | None:
        # Qwen 3.x поддерживает нативный toggle thinking через enable_thinking.
        # Оставляем пустым, чтобы не переопределять поведение по умолчанию.
        return None


class GeminiAdapter(_EnvMixin, LLMConfigAdapter):
    """Адаптер для моделей Google Gemini (OpenAI-compatible endpoint)."""

    @property
    def provider_name(self) -> str:
        return "gemini"

    def get_api_base(self) -> str:
        return self._clean_env(os.getenv("LLM_BASE_URL", ""))

    def get_api_key(self) -> str:
        return self._clean_env(
            os.getenv("GEMINI_API_KEY") or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        )

    def wire_model_name(self, model_name: str) -> str:
        return model_name.split("/", 1)[-1]


_QWEN_KEYWORDS = ("qwen", "dashscope")
_GEMINI_KEYWORDS = ("gemini", "gemma")


def create_adapter(model_name: str | None = None) -> LLMConfigAdapter:
    """Фабрика: определяет тип модели по наименованию и возвращает адаптер.

    Поддерживает:
      - qwen* / dashscope/* -> QwenAdapter
      - gemini* / gemma*    -> GeminiAdapter
      - всё остальное       -> OpenAIAdapter (fallback)

    Имя модели может содержать route-префикс вида ``provider/model``.
    """
    name = (model_name or "").lower().strip()
    if not name:
        return OpenAIAdapter()

    slug = name.split("/", 1)[-1]
    if any(name.startswith(kw + "/") for kw in _QWEN_KEYWORDS) or slug.startswith(_QWEN_KEYWORDS):
        return QwenAdapter()
    if any(name.startswith(kw + "/") for kw in _GEMINI_KEYWORDS) or slug.startswith(_GEMINI_KEYWORDS):
        return GeminiAdapter()
    return OpenAIAdapter()
