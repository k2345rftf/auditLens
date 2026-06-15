"""Ranking Agent — рейтинг субъектов сравнения.

Запускается ПОСЛЕ researcher и reviews (зависит от них). Получает в context
сводку уже собранных фактов/жалоб, строит рейтинг с обоснованием.

Гибридный подход:
  • LLM оценивает по совокупности (цена + UX + надёжность), опираясь на факты.
  • НЕ выдумывает оценки — каждое место обосновано конкретными дельтами/жалобами.
  • Честно помечает «недостаточно данных» вместо угадывания.
"""
from __future__ import annotations

import logging

from ..base_agent import BaseAgent
from ..knowledge_bundle import Ranking, RankEntry
from ..tools.tool_specs import RESEARCHER_TOOLS  # ranking тоже может дозаняться

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — ranking-агент для аудиторской платформы. Строишь рейтинг
субъектов (банков/продуктов) по совокупности критериев.

Тебе уже переданы в context собранные факты и жалобы по каждому субъекту
(секция «СОБРАННЫЕ ДАННЫЕ»). Это ТВОЯ ГЛАВНАЯ ОПОРА — ранк по этим данным.

КРИТИЧНО: НЕ начинай с web_search/read_url, если в контексте достаточно
фактов (≥2 на субъект). Ранжируй ПО ПЕРЕДАННЫМ ДАННЫМ. Дозаняться инструментами
разрешено ТОЛЬКО если по субъекту явный data_gap (нет ни одного факта).

КРИТЕРИИ (адаптируй под тему):
  • Для тарифного продукта: цена, ставка, требования, гибкость.
  • Для функции (как автоперевод): комиссии, гибкость настройки, лимиты, UX.
  • Для качества обслуживания: время ответа, % решения проблем, жалобы.

ПРАВИЛА:
  • Каждый ранг 1-2 предложениями обоснования СО ССЫЛКАМИ [N].
  • Если по субъекту мало данных — помечай data_gap=true и ставь в конец,
    не угадывай место.
  • Учитывай ЖАЛОБЫ: субъект с большим % neg-отзывов ниже.
  • Учитывай ИНСАЙТЫ (напр. «цены уравнены регулятором» — тогда рейтинг по
    гибкости, а не по цене — это и есть обоснование критерия).
  • Score 0-10, где 10 = лучший. У data_gap — низкий score с пометкой.

ВЫХОД (строгий JSON):
{
  "ranking": {
    "criterion": "по совокупности цена + гибкость + надёжность (учитывая уравниловку цен регулятором)",
    "entries": [
      {"subject":"Т-Банк","rank":1,"score":9.2,
       "rationale":"Наиболее гибкий: триггер по остатку + расписание, отмена в приложении [6]. Жалоб мало [9].",
       "evidence_ns":[6,9],"data_gap":false},
      {"subject":"Газпромбанк","rank":5,"score":4.0,
       "rationale":"Данных по автопереводу не найдено — нельзя оценить.",
       "evidence_ns":[],"data_gap":true}
    ]
  },
  "summary":"Рейтинг построен по N фактам и M жалобам. ..."
}
"""


class RankingAgent(BaseAgent):
    SYSTEM_PROMPT = SYSTEM_PROMPT
    TOOLS = RESEARCHER_TOOLS  # может дозаняться если не хватает данных
    # Аналитический синтез рейтинга с обоснованием — сильная модель.
    MODEL_TIER = "smart"

    async def _integrate(self, artifacts: dict) -> None:
        r = artifacts.get("ranking")
        if not isinstance(r, dict):
            return
        entries = []
        for e in (r.get("entries") or []):
            if not isinstance(e, dict):
                continue
            subject = str(e.get("subject") or "").strip()
            if not subject:
                continue
            # LLM видит метки в промпте («Сбербанк») и возвращает их же; bundle
            # держит slug'и (sberbank). Канонизируем — иначе ranking-запись не
            # сматчится с фактами/жалобами и не покажется с правильной меткой.
            subject = self.bundle.canonical_subject(subject)
            try:
                rank = int(e.get("rank") or 0)
            except (TypeError, ValueError):
                rank = 0
            if rank <= 0:
                continue
            try:
                score = float(e.get("score") or 0)
            except (TypeError, ValueError):
                score = 0
            entries.append(RankEntry(
                subject=subject,
                rank=rank,
                score=score,
                rationale=str(e.get("rationale") or ""),
                evidence_ns=[int(n) for n in (e.get("evidence_ns") or [])
                              if str(n).isdigit()][:8],
                data_gap=bool(e.get("data_gap")),
            ))
        if entries:
            self.bundle.ranking = Ranking(
                criterion=str(r.get("criterion") or ""),
                entries=entries,
            )
