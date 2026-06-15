"""Market Agent — рыночный контекст: доли, тренды, реформы, позиция банка.

Опциональный агент. Зову когда вопрос подразумевает контекст рынка:
«как Сбер vs рынок», «позиция банка», «доля», «кто лидер», «тренды».
"""
from __future__ import annotations

import logging

from ..base_agent import BaseAgent
from ..knowledge_bundle import Insight, CoverageNote
from ..tools.tool_specs import MARKET_TOOLS

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — market-агент для аудиторской платформы. Собираешь
рыночный контекст: доли рынка, позиции банков, тренды, реформы, прогнозы.

ИСТОЧНИКИ:
  • run_sql по v_offer_current / v_sber_vs_market — структурированные сравнения.
  • web_search "{тема} рынок доля тренд рейтинг 2025" + site:forbes.ru,
    site:rbc.ru, site:banki.ru.
  • read_url на статьи-обзоры рынка.

ВЫХОД (строгий JSON):
{
  "facts": [
    {"subject":"рынок","attribute":"доля Сбербанка по переводам",
     "value":"≈45%","source_n":3,"tags":["market_share"]}
  ],
  "insights": [
    {"headline":"Сбер лидер по объёму, но проигрывает по UX",
     "explanation":"...","evidence_ns":[3,5]}
  ],
  "summary":"..."
}
"""


class MarketAgent(BaseAgent):
    SYSTEM_PROMPT = SYSTEM_PROMPT
    TOOLS = MARKET_TOOLS
    # Опциональный рыночный контекст — наименее критичный → быстрая.
    MODEL_TIER = "fast"

    async def _integrate(self, artifacts: dict) -> None:
        from ..knowledge_bundle import Fact
        for f in (artifacts.get("facts") or []):
            if not isinstance(f, dict):
                continue
            try:
                src_n = int(f.get("source_n") or 0)
            except (TypeError, ValueError):
                src_n = 0
            if src_n <= 0:
                continue
            subject = str(f.get("subject") or "").strip()
            attr = str(f.get("attribute") or "").strip()
            value = str(f.get("value") or "").strip()
            if not subject or not attr:
                continue
            self.bundle.add_fact(Fact(
                subject=subject, attribute=attr, value=value,
                source_n=src_n,
                verbatim=str(f.get("verbatim") or "")[:400],
                confidence=float(f.get("confidence") or 0.6),
                tags=[str(t) for t in (f.get("tags") or [])][:5],
            ))
        for ins in (artifacts.get("insights") or []):
            if not isinstance(ins, dict):
                continue
            headline = str(ins.get("headline") or "").strip()
            if not headline:
                continue
            self.bundle.insights.append(Insight(
                headline=headline,
                explanation=str(ins.get("explanation") or ""),
                evidence_ns=[int(n) for n in (ins.get("evidence_ns") or [])
                              if str(n).isdigit()][:8],
                impact=str(ins.get("impact") or ""),
            ))
