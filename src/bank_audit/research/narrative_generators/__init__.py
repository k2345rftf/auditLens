"""Narrative generators — per-section LLM-генераторы текста аудит-отчёта.

Каждый модуль = одна секция (key_findings, pricing_breakdown, ...).
Все генераторы принимают:
  • list[Fact]    — только релевантные для секции/банка факты
  • Section       — мета-описание секции (title, focus, audit_relevance)
  • Entities      — список банков
  • Sources index — для верификации цитирования

Все генераторы возвращают markdown-фрагмент с:
  • заголовком ## или ###
  • связным текстом (а не bullet-list-ом)
  • [N] цитированием на каждом утверждении с числом
  • numeric guard: каждое число в тексте должно существовать во входных фактах
"""
from .base import (
    NarrativeContext,
    verify_numbers_in_text,
    enforce_citations,
    parse_json_object,
    strip_markdown_fences,
)
from . import key_findings
from . import per_entity_breakdown
from . import pricing_breakdown
from . import regulatory_box
from . import cant_do_box
from . import requirements_box
from . import digital_channels
from . import risks_recommendations
from . import conflict_explainer
from . import government_programs

__all__ = [
    "NarrativeContext",
    "verify_numbers_in_text",
    "enforce_citations",
    "parse_json_object",
    "strip_markdown_fences",
    "key_findings",
    "per_entity_breakdown",
    "pricing_breakdown",
    "regulatory_box",
    "cant_do_box",
    "requirements_box",
    "digital_channels",
    "risks_recommendations",
    "conflict_explainer",
    "government_programs",
]
