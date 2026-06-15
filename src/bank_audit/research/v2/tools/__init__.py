"""Tools package — обёртки над существующей инфраструктурой для агентов v2.

Каждый tool:
  • принимает простой dict (function-calling args)
  • возвращает JSON-строку для LLM
  • пишет в SourceRegistry bundle (для цитирования)
  • пассивно индексирует web-находки в БД (document/review) — future-proofing

Агенты используют эти tools через BaseAgent.tool-loop, не зная о деталях
реализации (web backend, парсер, SQL safety и т.д.).
"""
