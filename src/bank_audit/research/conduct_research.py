"""Advertising-Conduct Risk Research — РЕЖИМ «риск поведения по рекламе».

Принципиально отличается от product-comparison pipeline:
  • Сущность анализа — не «банки для сравнения», а КЕЙС (штраф / жалоба).
  • Источники — ФАС/УФАС/суды/правовые-новости/агрегаторы жалоб,
    а НЕ страницы продуктов банков.
  • Извлекаем CASE-records: банк, дата, регулятор, статья 38-ФЗ/КоАП,
    нарушение, сумма штрафа, итог, дословная цитата, источник.
  • Вывод — реестр кейсов по продукту, а не матрица атрибутов.

ГЛАВНОЕ — анти-галлюцинация юридических фактов:
  Прошлый pipeline выдумывал «ст. 14.1 закона 38-ФЗ, штраф 300-1000 тыс».
  Здесь: статья закона и сумма попадают в отчёт ТОЛЬКО если дословно
  присутствуют в источнике (verbatim_quote обязателен). Иначе — пусто/skip.

Переиспользует инфраструктуру: web_search (ddgs), fetch, relevance-windowing,
LLM-клиент, pdf_export.
"""
from __future__ import annotations
import asyncio, json, logging, os, re
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor

from openai import AsyncOpenAI

from ..ai.analyst import LLM_BASE_URL, LLM_API_KEY
from ..ai.deep_research import _patch_client_reasoning_effort, normalize_question
from ..rag.web_search import search as web_search
from ..rag.fetcher import fetch as fetch_url
from .fact_extractor import _relevant_excerpt
from .narrative_generators.base import parse_json_object, _extract_numbers

log = logging.getLogger(__name__)


# ── 16 продуктовых категорий из письма (с поисковыми синонимами) ───────────
CONDUCT_PRODUCTS: dict[str, dict] = {
    "credit_card":      {"label": "Кредитная карта",
                          "syn": ["кредитная карта", "кредитка", "кредитную карту"]},
    "consumer_loan":    {"label": "Потребительский кредит",
                          "syn": ["потребительский кредит", "потребкредит", "кредит наличными"]},
    "mortgage":         {"label": "Ипотека",
                          "syn": ["ипотека", "ипотечный кредит", "ипотеку"]},
    "auto_loan":        {"label": "Автокредит",
                          "syn": ["автокредит", "кредит на автомобиль"]},
    "edu_loan":         {"label": "Образовательный кредит",
                          "syn": ["образовательный кредит", "кредит на образование", "студенческий кредит"]},
    "broker_account":   {"label": "Брокерский счёт",
                          "syn": ["брокерский счёт", "брокерский счет", "брокерское обслуживание"]},
    "iis":              {"label": "ИИС",
                          "syn": ["ИИС", "индивидуальный инвестиционный счёт", "индивидуальный инвестиционный счет"]},
    "securities":       {"label": "Инвестиционные продукты с ценными бумагами",
                          "syn": ["ценные бумаги", "облигации", "акции", "ПИФ", "инвестиции реклама"]},
    "salary":           {"label": "Зарплатные продукты и сервисы",
                          "syn": ["зарплатный проект", "зарплатная карта", "зарплатный сервис"]},
    "debit_card":       {"label": "Дебетовые карты и счета",
                          "syn": ["дебетовая карта", "детская карта", "молодёжная карта", "карта Аэрофлот"]},
    "deposit":          {"label": "Вклады, депозиты и накопительные счета",
                          "syn": ["вклад", "депозит", "накопительный счёт", "накопительный счет"]},
    "subscription":     {"label": "Подписки / пакеты услуг",
                          "syn": ["подписка банка", "пакет услуг", "премиум-подписка", "пакет услуг банка"]},
    "transfers":        {"label": "Переводы (в т.ч. трансграничные)",
                          "syn": ["денежные переводы", "трансграничные переводы", "перевод за рубеж", "СБП"]},
    "insurance":        {"label": "Продукты страхования",
                          "syn": ["страхование", "страховой продукт", "страховка реклама", "ИСЖ", "НСЖ"]},
    "pension":          {"label": "Пенсионные продукты",
                          "syn": ["пенсионный продукт", "НПФ", "пенсионные накопления", "пенсионный план"]},
    "premium":          {"label": "Премиальное обслуживание",
                          "syn": ["премиальное обслуживание", "private banking", "пакет привилегий", "премиум-обслуживание"]},
}


# Высокоточные маркеры продукта — для отсева кросс-контаминации кейсов.
# Кейс относится к продукту P, если его текст содержит маркер P. Если содержит
# СИЛЬНЫЙ маркер ДРУГОГО продукта и НЕ содержит маркера P — это чужой кейс (drop).
# Маркеры подобраны на высокую точность (минимум ложных пересечений).
PRODUCT_MARKERS: dict[str, list[str]] = {
    "credit_card":    ["кредитн карт", "кредитную карт", "кредитной карт",
                        "кредитка", "кредитки", "кредиток", "кредитную карту"],
    "consumer_loan":  ["потребительск", "потребкредит", "кредит наличными",
                        "кредита наличными", "нецелев кредит"],
    "mortgage":       ["ипотек", "ипотеч"],
    "auto_loan":      ["автокредит", "кредит на автомобиль", "кредита на авто",
                        "автомобильн кредит"],
    "edu_loan":       ["образовательн кредит", "кредит на образован",
                        "студенческ кредит", "образовательн заём"],
    "broker_account": ["брокерск"],
    "iis":            ["иис", "индивидуальн инвестицион"],
    "securities":     ["ценн бумаг", "облигац", "брокер", "пиф ", "фондов рынок"],
    "salary":         ["зарплатн проект", "зарплатн карт", "зарплатн клиент"],
    "debit_card":     ["дебетов карт", "детск карт", "молодёжн карт",
                        "молодежн карт", "карт аэрофлот"],
    "deposit":        ["вклад", "депозит", "накопительн счет", "накопительн счёт"],
    "subscription":   ["подписк", "пакет услуг", "премиум-подписк"],
    "transfers":      ["денежн перевод", "трансгранич", "перевод за рубеж",
                        "перевод по сбп", "перевод средств"],
    "insurance":      ["страхован", "страхов продукт", "осаго", "каско",
                        "исж", "нсж", "страховк"],
    "pension":        ["пенсионн продукт", "пенсионн накоплен", "нпф",
                        "пенсионн план", "негосударственн пенси"],
    "premium":        ["премиальн обслуж", "private banking", "пакет привилег",
                        "премиум-обслуж", "vip-обслуж", "вип-обслуж"],
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower())


# Концепт-группы: слово-концепт → множество продуктов-«владельцев».
# Используется для классификации кейса: если упомянут концепт чужого продукта
# и НЕ упомянут концепт целевого — кейс чужой (отсев). Это ловит «голые»
# карта/кредит, которые точечные маркеры пропускали (утечка в страхование/пенсии).
CONCEPT_GROUPS: dict[str, set[str]] = {
    # карты
    "кредитн карт": {"credit_card"}, "кредитка": {"credit_card"},
    "кредитную карт": {"credit_card"}, "кредитной карт": {"credit_card"},
    "дебетов карт": {"debit_card"}, "детск карт": {"debit_card"},
    "молодёжн карт": {"debit_card"}, "молодежн карт": {"debit_card"},
    "карт аэрофлот": {"debit_card"},
    "карт": {"credit_card", "debit_card", "salary"},   # голая «карта» — карточная семья
    # кредиты
    "потребительск": {"consumer_loan"}, "кредит наличными": {"consumer_loan"},
    "потребкредит": {"consumer_loan"}, "персональн кредит": {"consumer_loan"},
    "автокредит": {"auto_loan"}, "на автомобиль": {"auto_loan"},
    "ипотек": {"mortgage"}, "ипотеч": {"mortgage"},
    "образовательн кредит": {"edu_loan"}, "студенческ кредит": {"edu_loan"},
    # NB: общий «кредит» обрабатывается отдельно (ниже), чтобы подстрока
    # «кредит» внутри «автокредит»/«потребкредит» не путала классификацию.
    # сбережения
    "вклад": {"deposit"}, "депозит": {"deposit"}, "накопительн": {"deposit"},
    "сберегательн счет": {"deposit"}, "сберегательн счёт": {"deposit"},
    # инвестиции
    "брокер": {"broker_account"}, "иис": {"iis"},
    "индивидуальн инвестицион": {"iis"},
    "облигац": {"securities"}, "ценн бумаг": {"securities"},
    "акци": {"securities"}, "пиф": {"securities"}, "фондов": {"securities"},
    # прочее
    "зарплатн": {"salary"},
    "подписк": {"subscription"}, "пакет услуг": {"subscription"},
    "перевод": {"transfers"}, "трансгранич": {"transfers"}, " сбп": {"transfers"},
    "страхов": {"insurance"}, "осаго": {"insurance"}, "каско": {"insurance"},
    "исж": {"insurance"}, "нсж": {"insurance"}, "страховк": {"insurance"},
    "пенси": {"pension"}, "нпф": {"pension"},
    "премиальн": {"premium"}, "private banking": {"premium"},
    "привилег": {"premium"}, "vip-обслуж": {"premium"}, "вип-обслуж": {"premium"},
}


def classify_case_product(text: str, target_key: str) -> str:
    """Возвращает 'own' | 'foreign' | 'neutral' для кейса относительно продукта.

    own     — упомянут концепт целевого продукта → оставить
    foreign — упомянут концепт ДРУГОГО продукта и НЕ целевого → отсеять
    neutral — нет продуктовых концептов вообще (общая реклама) → оставить
    """
    t = _norm(text)
    owners: set[str] = set()
    for concept, group in CONCEPT_GROUPS.items():
        if concept in t:
            owners |= group
    # Общий «кредит/заём» — только если НЕ покрыт специфичным кредит-концептом
    # (автокредит/потребкредит/ипотека/образовательный/кредитка), иначе подстрока
    # «кредит» внутри «автокредит» ложно расширяла бы владельцев.
    _SPECIFIC_LOAN = ("автокредит", "потребительск", "потребкредит",
                       "персональн кредит", "кредит наличными", "ипотек", "ипотеч",
                       "образовательн кредит", "студенческ кредит",
                       "кредитн карт", "кредитка")
    if re.search(r"кредит|за[её]м", t) and not any(s in t for s in _SPECIFIC_LOAN):
        owners |= {"consumer_loan", "auto_loan", "mortgage", "edu_loan", "credit_card"}
    if target_key in owners:
        return "own"
    if owners:           # есть концепт(ы) чужого продукта, целевого нет
        return "foreign"
    return "neutral"


# Домены, релевантные enforcement/жалобам (для приоритизации источников)
ENFORCEMENT_DOMAINS = {
    "fas.gov.ru": 1.0, "br.fas.gov.ru": 1.0, "moscow.fas.gov.ru": 1.0,
    "consultant.ru": 0.95, "garant.ru": 0.95, "pravo.gov.ru": 1.0,
    "pravo.ru": 0.9, "kad.arbitr.ru": 0.95, "sudact.ru": 0.9,
    "kommersant.ru": 0.85, "rbc.ru": 0.85, "vedomosti.ru": 0.85,
    "interfax.ru": 0.85, "tass.ru": 0.85, "banki.ru": 0.8,
    "frankmedia.ru": 0.8, "frankrg.com": 0.8, "asn-news.ru": 0.8,
    "klerk.ru": 0.75, "x-compliance.ru": 0.8, "law.ru": 0.8,
    "advertology.ru": 0.75, "sostav.ru": 0.75, "adindex.ru": 0.75,
}

# Домены-жалобы (народные рейтинги, отзывы)
COMPLAINT_DOMAINS = {
    "banki.ru": 0.85, "sravni.ru": 0.8, "irecommend.ru": 0.6,
    "otzovik.com": 0.6, "vc.ru": 0.6, "pikabu.ru": 0.5,
}


@dataclass
class ConductCase:
    """Один кейс: штраф / предписание / контрреклама / жалоба."""
    bank: str = ""
    kind: str = "штраф"               # штраф/предписание/контрреклама/предупреждение/жалоба
    date: str = ""                    # «2025-03» / «2025» как в источнике
    regulator: str = ""               # ФАС / УФАС Москвы / суд / ЦБ
    law_basis: str = ""               # ст. 5/28 38-ФЗ / КоАП 14.3 — ТОЛЬКО из источника
    violation: str = ""               # суть нарушения
    ad_channel: str = ""              # ТВ/интернет/наружная/радио — если указано
    amount_rub: float | None = None   # сумма — ТОЛЬКО из источника
    outcome: str = ""                 # итог
    verbatim_quote: str = ""          # дословная цитата (обязательна)
    source_idx: int = 0
    source_url: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ConductComplaint:
    """Обращение/жалоба клиента на рекламу продукта."""
    bank: str = ""
    summary: str = ""                 # суть обращения
    ad_issue: str = ""                # что не так с рекламой
    response: str = ""                # ответ банка, если есть
    date: str = ""
    verbatim_quote: str = ""
    source_idx: int = 0
    source_url: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ════════════════════════════════════════════════════════════════════
# 1) QUERY PLANNING — enforcement + complaints
# ════════════════════════════════════════════════════════════════════


def plan_conduct_queries(product_key: str) -> list[str]:
    """Генерирует поисковые запросы под enforcement + жалобы для продукта."""
    p = CONDUCT_PRODUCTS[product_key]
    syns = p["syn"]
    main = syns[0]
    queries: list[str] = []
    # Штрафы ФАС/УФАС по годам
    for year in ("2025", "2026"):
        queries.append(f"ФАС штраф банк реклама {main} {year}")
        queries.append(f"УФАС банк ненадлежащая реклама {main} {year}")
    queries.append(f"ФАС решение банк недостоверная реклама {main}")
    queries.append(f"банк оштрафован реклама {main} нарушение закона о рекламе")
    queries.append(f"контрреклама банк {main} ФАС")
    queries.append(f"суд штраф банк реклама {main} 38-ФЗ")
    # Конкретные синонимы
    for s in syns[1:3]:
        queries.append(f"ФАС банк реклама {s} штраф")
    # Жалобы клиентов
    queries.append(f"жалоба клиента реклама банка {main} ввела в заблуждение")
    queries.append(f"обращение реклама банк {main} обман недостоверная")
    queries.append(f"site:banki.ru реклама {main} обман отзыв")
    return queries


# ════════════════════════════════════════════════════════════════════
# 2) SOURCE COLLECTION
# ════════════════════════════════════════════════════════════════════


@dataclass
class ConductSource:
    n: int
    url: str
    title: str
    domain: str
    text: str
    trust: float
    snippet: str = ""


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _source_trust(domain: str) -> float:
    if domain in ENFORCEMENT_DOMAINS:
        return ENFORCEMENT_DOMAINS[domain]
    if domain in COMPLAINT_DOMAINS:
        return COMPLAINT_DOMAINS[domain]
    # поддомены *.fas.gov.ru
    if domain.endswith(".fas.gov.ru") or domain == "fas.gov.ru":
        return 1.0
    return 0.5


def collect_conduct_sources(product_key: str, max_sources: int = 22,
                              product_terms: list[str] | None = None) -> list[ConductSource]:
    """Поиск + загрузка источников для продукта. Sync (вызывать в executor)."""
    queries = plan_conduct_queries(product_key)
    # 1) Поиск по всем запросам
    seen_urls: set[str] = set()
    ranked: list[tuple[float, str, str, str]] = []   # (trust, url, title, snippet)
    for q in queries:
        try:
            results = web_search(q, max_results=6) or []
        except Exception as e:
            log.info("[conduct] search failed %r: %s", q, e)
            continue
        for r in results:
            url = r.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            dom = r.get("domain") or _domain_of(url)
            ranked.append((_source_trust(dom), url, r.get("title", ""),
                            r.get("snippet", "")))
    # 2) Сортируем по trust, берём топ для загрузки
    ranked.sort(key=lambda x: -x[0])
    to_fetch = ranked[: max_sources + 8]

    # 3) Загружаем (HTTP, browser-fallback внутри fetch)
    terms = product_terms or CONDUCT_PRODUCTS[product_key]["syn"]

    def _fetch_one(item):
        trust, url, title, snippet = item
        try:
            r = fetch_url(url, prefer_browser=False)
            raw = r.content or b""
            html = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else (raw or "")
            if not html or r.status != 200:
                return None
            from selectolax.parser import HTMLParser
            txt = HTMLParser(html).text(separator=" ")
            txt = re.sub(r"\s+", " ", txt).strip()
            if len(txt) < 200:
                return None
            # релевантная выборка (новости/решения большие)
            excerpt = _relevant_excerpt(txt, [t.lower() for t in terms] +
                                          ["штраф", "фас", "реклама", "нарушение", "жалоба"],
                                          budget=9000)
            return (trust, url, title, _domain_of(url), excerpt, snippet)
        except Exception as e:
            log.info("[conduct] fetch failed %s: %s", url, e)
            return None

    out: list[ConductSource] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(_fetch_one, to_fetch):
            if res:
                trust, url, title, dom, excerpt, snippet = res
                out.append(ConductSource(n=0, url=url, title=title[:160],
                                          domain=dom, text=excerpt, trust=trust,
                                          snippet=snippet[:300]))
            if len(out) >= max_sources:
                break
    # нумерация
    for i, s in enumerate(out, 1):
        s.n = i
    log.warning("[conduct] %s: %s sources collected (%s enforcement-trust)",
                 product_key, len(out),
                 sum(1 for s in out if s.trust >= 0.8))
    return out


# ════════════════════════════════════════════════════════════════════
# 3) CASE EXTRACTION — строгая анти-галлюцинация
# ════════════════════════════════════════════════════════════════════


CASE_SYSTEM_PROMPT = """Ты — юрист-аналитик ФАС-практики. Из текстов источников
извлекаешь РЕАЛЬНЫЕ кейсы по нарушению банками закона «О рекламе» (38-ФЗ):
штрафы, предписания, контрреклама, предупреждения, судебные решения.

СТРОЖАЙШЕЕ ПРАВИЛО — НИКАКИХ ВЫДУМОК:
  • Извлекай ТОЛЬКО то, что ПРЯМО написано в источнике.
  • Статью закона (law_basis) и сумму штрафа (amount_rub) указывай ТОЛЬКО
    если они ДОСЛОВНО присутствуют в тексте. Если не указано — оставь ПУСТО.
  • НЕ придумывай номера статей. НЕ оценивай «примерные» суммы.
  • Каждый кейс ОБЯЗАН иметь verbatim_quote — дословную цитату из источника
    (30-300 символов), на основании которой ты его извлёк.
  • Если в источнике нет конкретных кейсов нарушений рекламы — верни [].

Что извлекать (каждый кейс = JSON-объект):
  • bank          — банк-нарушитель (как в тексте). Если не банк — пропусти.
  • kind          — штраф / предписание / контрреклама / предупреждение / решение_суда
  • date          — дата/период как в источнике («2025», «март 2025», «2025-03»)
  • regulator     — кто вынес (ФАС / УФАС <город> / суд / ЦБ)
  • law_basis     — статья ТОЛЬКО если в тексте («ст. 5 38-ФЗ», «ч.1 ст.28»,
                    «КоАП ст. 14.3»). Нет в тексте → ""
  • violation     — суть нарушения (что не так с рекламой)
  • ad_channel    — канал рекламы если указан (ТВ/радио/интернет/наружная/...)
  • amount_rub    — сумма штрафа ЧИСЛОМ ТОЛЬКО если указана, иначе null
  • outcome       — итог (наложен/обжалуется/отменён/контрреклама размещена)
  • verbatim_quote— ДОСЛОВНАЯ цитата-основание
  • source_idx    — номер источника [N]

ВЫХОД: JSON-объект {"cases": [...]}. БЕЗ преамбулы, БЕЗ markdown-fences.
Если кейсов нет — {"cases": []}."""


COMPLAINT_SYSTEM_PROMPT = """Ты — аналитик клиентского опыта. Из источников
извлекаешь ОБРАЩЕНИЯ/ЖАЛОБЫ клиентов именно НА РЕКЛАМУ банковского продукта
(реклама ввела в заблуждение, скрытые условия, несоответствие обещанного).

ПРАВИЛА:
  • Только жалобы, связанные с РЕКЛАМОЙ/обещаниями/информированием, НЕ общие
    жалобы на сервис/приложение/очереди.
  • Каждая запись ОБЯЗАНА иметь verbatim_quote (дословно из источника).
  • НЕ выдумывай. Нет relevantных жалоб → {"complaints": []}.

Поля:
  • bank, summary (суть), ad_issue (что не так с рекламой),
    response (ответ банка если есть, иначе ""), date, verbatim_quote, source_idx

ВЫХОД: JSON {"complaints": [...]}. БЕЗ преамбулы, БЕЗ fences."""


def _sources_block_for_llm(sources: list[ConductSource], budget_each: int = 5500) -> str:
    parts = []
    for s in sources:
        parts.append(f"### Источник [{s.n}] — {s.title} ({s.domain})\n"
                      f"URL: {s.url}\n\n{s.text[:budget_each]}")
    return "\n\n---\n\n".join(parts)


async def _llm_json(client: AsyncOpenAI, model: str, system: str, user: str,
                     max_tokens: int = 4000, timeout: int = 90) -> dict | None:
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                max_tokens=max_tokens, temperature=0.0,
            ), timeout=timeout)
        return parse_json_object(resp.choices[0].message.content or "")
    except Exception as e:
        log.warning("[conduct] LLM failed: %s", e)
        return None


def _in_period(date_str: str, years=("2025", "2026")) -> bool:
    """True если дата в интересующем периоде ИЛИ год не определён.

    Письмо требует 2025-2026. Кейсы с явным годом < 2025 отбраковываем,
    недатированные оставляем (источник пришёл по запросу за 2025-2026)."""
    if not date_str:
        return True
    m = re.search(r"(19|20)\d{2}", date_str)
    if not m:
        return True
    return m.group(0) in years


def _verify_quote_in_sources(quote: str, sources_by_idx: dict[int, ConductSource],
                               idx: int) -> bool:
    """Грубая проверка: цитата (или её существенная часть) есть в тексте источника."""
    if not quote or idx not in sources_by_idx:
        return False
    src_text = sources_by_idx[idx].text.lower()
    q = re.sub(r"\s+", " ", quote.lower()).strip()
    if len(q) < 12:
        return False
    # проверяем по «ядру» цитаты (первые 40 символов и кусок из середины)
    head = q[:40]
    if head in src_text:
        return True
    # допускаем частичное совпадение по словам (нормализация пробелов в источнике)
    words = [w for w in re.findall(r"\w{4,}", q)][:8]
    hits = sum(1 for w in words if w in src_text)
    return hits >= max(3, len(words) * 0.5)


def _dedup_key(bank: str, quote: str) -> str:
    return (bank.lower().strip() + "|" +
            re.sub(r"\s+", " ", quote.lower())[:50])


# Числа с масштабом «тыс/млн/млрд» → реальное значение в рублях.
_MONEY_SCALE = re.compile(
    r"(\d{1,4}(?:[  .]\d{3})*(?:[.,]\d+)?)\s*"
    r"(тыс|тысяч|млн|миллион|млрд|миллиард)", re.IGNORECASE)


def _extract_money(text: str) -> set[float]:
    """Все денежные величины из текста, включая масштаб «500 тыс»→500000."""
    out: set[float] = set(_extract_numbers(text))
    for m in _MONEY_SCALE.finditer(text):
        raw = m.group(1).replace(" ", "").replace(" ", "").replace(".", "").replace(",", ".")
        try:
            val = float(raw)
        except ValueError:
            continue
        unit = m.group(2).lower()
        if unit.startswith("тыс"):
            val *= 1_000
        elif unit.startswith("млн") or unit.startswith("миллион"):
            val *= 1_000_000
        elif unit.startswith("млрд") or unit.startswith("миллиард"):
            val *= 1_000_000_000
        out.add(round(val, 2))
    return out


def _amount_in_source(amount: float, src_text: str) -> bool:
    """Сумма подтверждена источником (с учётом масштаба тыс/млн)."""
    nums = _extract_money(src_text)
    for n in nums:
        if n == 0:
            continue
        if abs(amount - n) < 1 or abs(amount - n) / n < 0.01:
            return True
    return False


def _parse_one_case(c: dict, by_idx: dict[int, ConductSource],
                     target_key: str | None = None) -> ConductCase | None:
    if not isinstance(c, dict):
        return None
    bank = str(c.get("bank") or "").strip()
    quote = str(c.get("verbatim_quote") or "").strip()
    try:
        idx = int(c.get("source_idx") or 0)
    except Exception:
        idx = 0
    # АНТИ-ГАЛЛЮЦИНАЦИЯ: кейс валиден только с цитатой, подтверждённой в источнике
    if not bank or not quote or not _verify_quote_in_sources(quote, by_idx, idx):
        return None
    if not _in_period(str(c.get("date") or "")):
        return None
    # PRODUCT-ФИЛЬТР: в отчёт по продукту попадают только кейсы, атрибутированные
    # ЭТОМУ продукту (own). foreign (чужой продукт) и neutral (generic-реклама
    # без продукта: «мобильное приложение», «лучший банк») — отсеиваются.
    # Точность важнее recall: каждый кейс в отчёте реально про целевой продукт.
    violation = str(c.get("violation") or "").strip()
    if target_key:
        cls = classify_case_product(violation + " " + quote + " " +
                                      str(c.get("ad_channel") or ""), target_key)
        if cls != "own":
            log.info("[conduct] DROP case (%s, not own): %s / %s",
                      cls, bank, violation[:50])
            return None
    amount = c.get("amount_rub")
    try:
        amount = float(amount) if amount not in (None, "", "null") else None
    except Exception:
        amount = None
    if amount is not None and not _amount_in_source(amount, by_idx[idx].text):
        amount = None
    return ConductCase(
        bank=bank, kind=str(c.get("kind") or "штраф").strip(),
        date=str(c.get("date") or "").strip(),
        regulator=str(c.get("regulator") or "").strip(),
        law_basis=str(c.get("law_basis") or "").strip(),
        violation=str(c.get("violation") or "").strip(),
        ad_channel=str(c.get("ad_channel") or "").strip(),
        amount_rub=amount, outcome=str(c.get("outcome") or "").strip(),
        verbatim_quote=quote[:300], source_idx=idx,
        source_url=by_idx[idx].url,
    )


async def _extract_cases_batch(client: AsyncOpenAI, model: str, product_label: str,
                                 batch: list[ConductSource],
                                 by_idx: dict[int, ConductSource],
                                 target_key: str | None = None) -> list[ConductCase]:
    block = _sources_block_for_llm(batch)
    # Промпт извлекает ВСЕ реальные кейсы (высокий recall). Продуктовую
    # атрибуцию (отсев кейсов про другой продукт) делает детерминированный
    # post-фильтр classify_case_product — он точнее и не теряет обобщённо
    # описанные кейсы по целевому продукту.
    user = (f"# Целевой продукт: {product_label}\n# Период интереса: 2025–2026\n\n"
            f"# ИСТОЧНИКИ\n{block}\n\n"
            f"Извлеки ВСЕ реальные кейсы нарушения банками закона о рекламе "
            f"(38-ФЗ) из этих источников — штрафы/предписания/контрреклама/суд. "
            f"В поле violation чётко указывай, реклама КАКОГО продукта нарушила "
            f"(карта, кредит, вклад, ипотека и т.п.). source_idx — номер [N]. "
            f"НЕ выдумывай статьи и суммы.")
    data = await _llm_json(client, model, CASE_SYSTEM_PROMPT, user, max_tokens=3500)
    if not data or "cases" not in data:
        return []
    res = []
    for c in (data.get("cases") or []):
        parsed = _parse_one_case(c, by_idx, target_key=target_key)
        if parsed:
            res.append(parsed)
    return res


async def extract_cases(client: AsyncOpenAI, model: str, product_label: str,
                          sources: list[ConductSource],
                          batch_size: int = 4,
                          target_key: str | None = None) -> list[ConductCase]:
    """Батчевое извлечение: источники по 4 параллельно → merge → dedup.

    target_key — ключ продукта для отсева кейсов про другие продукты.
    Стабильнее одного большого вызова (сбой одного батча не обнуляет всё)
    и тщательнее (каждый источник получает фокус модели)."""
    if not sources:
        return []
    by_idx = {s.n: s for s in sources}
    batches = [sources[i:i + batch_size] for i in range(0, len(sources), batch_size)]
    sem = asyncio.Semaphore(4)

    async def _one(b):
        async with sem:
            try:
                return await _extract_cases_batch(client, model, product_label, b,
                                                    by_idx, target_key=target_key)
            except Exception as e:
                log.info("[conduct] batch failed: %s", e)
                return []

    # ДВА прохода извлечения по всем батчам → merge+dedup. LLM недетерминирована
    # (MoE), за один проход часть кейсов теряется; второй проход их добирает.
    # Это поднимает recall и стабилизирует результат между запусками.
    results1 = await asyncio.gather(*[_one(b) for b in batches])
    results2 = await asyncio.gather(*[_one(b) for b in batches])
    seen: set[str] = set()
    out: list[ConductCase] = []
    for batch_res in results1 + results2:
        for c in batch_res:
            k = _dedup_key(c.bank, c.verbatim_quote)
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
    log.warning("[conduct] %s: %s cases extracted (verified, %s batches ×2 прохода)",
                 product_label, len(out), len(batches))
    return out


async def _extract_complaints_batch(client: AsyncOpenAI, model: str, product_label: str,
                                      batch: list[ConductSource],
                                      by_idx: dict[int, ConductSource]) -> list[ConductComplaint]:
    block = _sources_block_for_llm(batch)
    user = (f"# Продукт: {product_label}\n\n# ИСТОЧНИКИ\n{block}\n\n"
            f"Извлеки жалобы клиентов именно НА РЕКЛАМУ продукта «{product_label}».")
    data = await _llm_json(client, model, COMPLAINT_SYSTEM_PROMPT, user, max_tokens=2500)
    if not data or "complaints" not in data:
        return []
    out = []
    for c in (data.get("complaints") or []):
        if not isinstance(c, dict):
            continue
        quote = str(c.get("verbatim_quote") or "").strip()
        try:
            idx = int(c.get("source_idx") or 0)
        except Exception:
            idx = 0
        if not quote or not _verify_quote_in_sources(quote, by_idx, idx):
            continue
        out.append(ConductComplaint(
            bank=str(c.get("bank") or "").strip(),
            summary=str(c.get("summary") or "").strip(),
            ad_issue=str(c.get("ad_issue") or "").strip(),
            response=str(c.get("response") or "").strip(),
            date=str(c.get("date") or "").strip(),
            verbatim_quote=quote[:300], source_idx=idx,
            source_url=by_idx[idx].url,
        ))
    return out


async def extract_complaints(client: AsyncOpenAI, model: str, product_label: str,
                               sources: list[ConductSource],
                               batch_size: int = 4) -> list[ConductComplaint]:
    if not sources:
        return []
    csrc = [s for s in sources if s.domain in COMPLAINT_DOMAINS] or sources
    by_idx = {s.n: s for s in sources}
    batches = [csrc[i:i + batch_size] for i in range(0, len(csrc), batch_size)]
    sem = asyncio.Semaphore(4)

    async def _one(b):
        async with sem:
            try:
                return await _extract_complaints_batch(client, model, product_label, b, by_idx)
            except Exception as e:
                log.info("[conduct] complaint batch failed: %s", e)
                return []

    results = await asyncio.gather(*[_one(b) for b in batches])
    seen: set[str] = set()
    out: list[ConductComplaint] = []
    for batch_res in results:
        for c in batch_res:
            k = _dedup_key(c.bank, c.verbatim_quote)
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
    log.warning("[conduct] %s: %s complaints extracted (%s batches)",
                 product_label, len(out), len(batches))
    return out


# ════════════════════════════════════════════════════════════════════
# 4) REPORT RENDERER — реестр кейсов
# ════════════════════════════════════════════════════════════════════


def _fmt_amount(a: float | None) -> str:
    if a is None:
        return "—"
    return f"{int(a):,}".replace(",", " ") + " ₽"


def render_conduct_report(product_label: str, cases: list[ConductCase],
                            complaints: list[ConductComplaint],
                            sources: list[ConductSource]) -> str:
    lines: list[str] = []
    lines.append(f"# Риск поведения по рекламе: {product_label}")
    lines.append("")
    lines.append("**Период:** 2025–2026 · открытые источники · "
                  "закон №38-ФЗ «О рекламе»")
    lines.append("")

    # ── Сводка ──
    n_fines = sum(1 for c in cases if c.amount_rub is not None)
    total = sum(c.amount_rub for c in cases if c.amount_rub is not None)
    banks = sorted({c.bank for c in cases if c.bank})
    lines.append("## 📊 Сводка")
    lines.append("")
    if cases or complaints:
        lines.append(f"- Найдено **{len(cases)} кейсов** регуляторных нарушений "
                      f"и **{len(complaints)} обращений/жалоб** на рекламу.")
        if n_fines:
            lines.append(f"- Из них с указанной суммой штрафа: **{n_fines}**, "
                          f"суммарно **{_fmt_amount(total)}**.")
        if banks:
            lines.append(f"- Фигурируют банки: {', '.join(banks)}.")
        arts = sorted({c.law_basis for c in cases if c.law_basis})
        if arts:
            lines.append(f"- Применённые нормы: {', '.join(arts)}.")
    else:
        lines.append("- За период **2025–2026 в открытых источниках не найдено "
                      "подтверждённых кейсов** нарушения рекламы по данному продукту. "
                      "Это не означает отсутствия нарушений — требуется запрос в ФАС "
                      "и внутренние данные комплаенс.")
    lines.append("")

    # ── 1. Штрафы и решения ──
    lines.append("## ⚖️ 1. Штрафы и решения регуляторов (38-ФЗ)")
    lines.append("")
    if cases:
        lines.append("| Банк | Дата | Регулятор | Норма | Нарушение | Штраф | Итог | Ист. |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for c in cases:
            row = (f"| {c.bank} | {c.date or '—'} | {c.regulator or '—'} | "
                    f"{c.law_basis or '—'} | {(c.violation or '—')[:95]} | "
                    f"{_fmt_amount(c.amount_rub)} | {(c.outcome or '—')[:55]} | "
                    f"[{c.source_idx}] |")
            lines.append(row)
        lines.append("")
        # детально-цитаты по заметным кейсам
        lines.append("**Детально (с цитатами из источников):**")
        lines.append("")
        for c in cases:
            head = f"**{c.bank}**"
            if c.date:
                head += f", {c.date}"
            if c.regulator:
                head += f" — {c.regulator}"
            lines.append(f"- {head}: {c.violation or 'нарушение рекламы'}"
                          + (f" ({c.ad_channel})" if c.ad_channel else "")
                          + (f". Штраф {_fmt_amount(c.amount_rub)}" if c.amount_rub else "")
                          + (f". {c.outcome}" if c.outcome else "")
                          + f" [{c.source_idx}]")
            if c.verbatim_quote:
                q = c.verbatim_quote.replace("*", "").replace("«", "").replace("»", "").strip()
                lines.append(f"  *«{q}»*")
        lines.append("")
    else:
        lines.append("Подтверждённых решений ФАС/УФАС/судов в открытых источниках за период не выявлено.")
        lines.append("")

    # ── 2. Обращения и жалобы ──
    lines.append("## 🗣 2. Обращения и жалобы клиентов на рекламу")
    lines.append("")
    if complaints:
        lines.append("| Банк | Суть обращения | Проблема рекламы | Ответ банка | Ист. |")
        lines.append("|---|---|---|---|---|")
        for c in complaints:
            lines.append(f"| {c.bank or '—'} | {(c.summary or '—')[:80] } | "
                          f"{(c.ad_issue or '—')[:70]} | {(c.response or '—')[:40]} | "
                          f"[{c.source_idx}] |")
        lines.append("")
        for c in complaints[:8]:
            if c.verbatim_quote:
                lines.append(f"- **{c.bank or 'Клиент'}**: {c.ad_issue or c.summary} "
                              f"[{c.source_idx}]")
                q = c.verbatim_quote.replace("*", "").replace("«", "").replace("»", "").strip()
                lines.append(f"  *«{q}»*")
        lines.append("")
    else:
        lines.append("Обращений именно на рекламу (а не на сервис) в открытых источниках за период не выявлено.")
        lines.append("")

    # ── 3. Риски и рекомендации ──
    lines.append("## ⚠️ 3. Риски и рекомендации аудитору")
    lines.append("")
    if cases or complaints:
        lines.append("- Сверить рекламные материалы банка по продукту "
                      f"«{product_label}» с выявленными нарушениями-аналогами.")
        if any(c.law_basis for c in cases):
            lines.append("- Проверить соблюдение норм, по которым уже выносились "
                          "решения (см. колонку «Норма»).")
        lines.append("- Запросить в ФАС/УФАС полный реестр дел по банку за период "
                      "(открытые источники дают неполную картину).")
        lines.append("- Проверить наличие и сроки ответов банка на обращения клиентов.")
    else:
        lines.append("- Отсутствие публичных кейсов не равно отсутствию риска: "
                      "сделать прямой запрос в ФАС и поднять внутренний реестр обращений.")
        lines.append(f"- Провести выборочный аудит рекламных материалов по продукту "
                      f"«{product_label}» на соответствие ст. 5, 28 закона 38-ФЗ.")
    lines.append("")

    # ── Источники ──
    lines.append(f"## 📚 Источники ({len(sources)})")
    lines.append("")
    for s in sources:
        trust = "●●●" if s.trust >= 0.9 else "●●○" if s.trust >= 0.7 else "○○○"
        lines.append(f"{s.n}. [{(s.title or s.url)[:90]}]({s.url}) — _{s.domain}_ {trust}")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# 5) ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════


def _make_client() -> AsyncOpenAI:
    client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
                          max_retries=4, timeout=180.0)
    return _patch_client_reasoning_effort(client)


async def run_conduct_research(product_key: str,
                                 client: AsyncOpenAI | None = None,
                                 model: str | None = None) -> dict:
    """Главная: для product_key возвращает {report_md, sources, cases, complaints}.

    sources — в формате, совместимом с export_report_to_pdf (n, url, title, domain,
    trust_score, source_kind).
    """
    if product_key not in CONDUCT_PRODUCTS:
        raise ValueError(f"unknown product_key {product_key}")
    product_label = CONDUCT_PRODUCTS[product_key]["label"]
    model = model or os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL_NAME",
                                                                 "gpt-4o-mini")
    own_client = client is None
    client = client or _make_client()

    loop = asyncio.get_event_loop()
    # 1) Сбор источников (sync в executor)
    sources = await loop.run_in_executor(
        None, collect_conduct_sources, product_key, 22,
        CONDUCT_PRODUCTS[product_key]["syn"])

    if not sources:
        report_md = render_conduct_report(product_label, [], [], [])
        return {"report_md": report_md, "sources": [], "cases": [], "complaints": []}

    # 2) Извлечение кейсов + жалоб (параллельно), с отсевом чужих продуктов
    cases, complaints = await asyncio.gather(
        extract_cases(client, model, product_label, sources, target_key=product_key),
        extract_complaints(client, model, product_label, sources),
    )

    # 3) Отсев источников до ЦИТИРУЕМЫХ + перенумерация (тугой список источников).
    cases, complaints, kept_sources = _prune_and_renumber(cases, complaints, sources)

    # 4) Рендер
    report_md = render_conduct_report(product_label, cases, complaints, kept_sources)

    # 5) sources для PDF
    pdf_sources = [{
        "n": s.n, "url": s.url, "title": s.title or s.url,
        "domain": s.domain, "trust_score": s.trust,
        "source_kind": "regulator" if s.trust >= 0.9 else
                        ("news_legal" if s.trust >= 0.75 else "aggregator"),
        "excerpts": [s.snippet] if s.snippet else [],
    } for s in kept_sources]

    log.warning("[conduct] DONE %s: %s cases, %s complaints, %s/%s sources cited",
                 product_label, len(cases), len(complaints),
                 len(kept_sources), len(sources))
    return {"report_md": report_md, "sources": pdf_sources,
            "cases": [c.to_dict() for c in cases],
            "complaints": [c.to_dict() for c in complaints],
            "product_label": product_label, "product_key": product_key}


def render_md_from_result(result: dict) -> str:
    """Пересобирает markdown из сохранённого result-dict БЕЗ обращения к API.

    Позволяет переотрисовать отчёт после правок рендерера, не тратя лимиты.
    Ожидает ключи: product_label, cases, complaints, sources (как в run_*)."""
    label = result.get("product_label", "")
    cases = [ConductCase(**{k: v for k, v in c.items()
                             if k in ConductCase.__dataclass_fields__})
             for c in result.get("cases", [])]
    complaints = [ConductComplaint(**{k: v for k, v in c.items()
                                       if k in ConductComplaint.__dataclass_fields__})
                  for c in result.get("complaints", [])]
    sources = [ConductSource(n=s.get("n", 0), url=s.get("url", ""),
                              title=s.get("title", ""), domain=s.get("domain", ""),
                              text="", trust=s.get("trust_score", 0.5),
                              snippet=(s.get("excerpts") or [""])[0])
               for s in result.get("sources", [])]
    return render_conduct_report(label, cases, complaints, sources)


def _prune_and_renumber(cases: list[ConductCase], complaints: list[ConductComplaint],
                          sources: list[ConductSource]):
    """Оставляет только процитированные источники, перенумеровывает 1..K,
    переписывает source_idx в кейсах/жалобах. Тугой, честный список источников."""
    used = sorted({c.source_idx for c in cases} | {c.source_idx for c in complaints})
    if not used:
        return cases, complaints, []
    by_old = {s.n: s for s in sources}
    remap: dict[int, int] = {}
    kept: list[ConductSource] = []
    for new_n, old_n in enumerate(used, 1):
        s = by_old.get(old_n)
        if not s:
            continue
        remap[old_n] = new_n
        # копия с новым номером
        kept.append(ConductSource(n=new_n, url=s.url, title=s.title,
                                    domain=s.domain, text=s.text, trust=s.trust,
                                    snippet=s.snippet))
    for c in cases:
        c.source_idx = remap.get(c.source_idx, c.source_idx)
    for c in complaints:
        c.source_idx = remap.get(c.source_idx, c.source_idx)
    return cases, complaints, kept
