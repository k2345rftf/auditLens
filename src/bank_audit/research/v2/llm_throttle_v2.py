"""LLM throttle для v2 — тонкая обёртка над существующим llm_throttle.

Переиспользуем тот же semaphore/backoff что и старый pipeline (он проверен
на Fireworks rate-limits). Не дублируем логику.
"""
from __future__ import annotations

from ..llm_throttle import patch_client_throttle  # noqa: F401  (re-export)
