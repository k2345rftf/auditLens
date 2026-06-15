"""Registry: agent_id → класс. Кондуктор отдаёт agent_id, orchestrator
строит инстанс через этот реестр."""
from __future__ import annotations

from ..base_agent import BaseAgent
from .researcher import ResearcherAgent
from .reviews import ReviewsAgent
from .regulatory import RegulatoryAgent
from .market import MarketAgent
from .ranking import RankingAgent

AGENT_REGISTRY: dict[str, type[BaseAgent]] = {
    "researcher": ResearcherAgent,
    "reviews": ReviewsAgent,
    "regulatory": RegulatoryAgent,
    "market": MarketAgent,
    "ranking": RankingAgent,
}
