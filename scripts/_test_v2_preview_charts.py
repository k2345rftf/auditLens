"""Дымовой тест §5a/§5b/§5c: ранняя отдача в v2-pipeline.

Без LLM-вызовов (мок-клиент) — проверяет чисто детерминированную сантехнику:
  1. bundle.extract_chart_specs() → корректные chart-specs из фактов;
  2. bundle.to_comparison_table() → markdown с данными;
  3. write_report(preview_emitted=True) через mock → аналитик НЕ получает в промпт
     готовую таблицу (дедуп раннего preview), а получает напоминание НЕ дублировать;
  4. write_report(preview_emitted=False) → таблица в промпте как раньше;
  5. импорты всех задетействованных модулей.

Запуск: python scripts/_test_v2_preview_charts.py
"""
import asyncio
import sys
from pathlib import Path

# Чтобы `import bank_audit...` работал из scripts/
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_bundle():
    """Синтетический bundle: 2 банка, 3 числовых атрибута + 1 нерелевантный."""
    from bank_audit.research.v2.knowledge_bundle import (
        KnowledgeBundle, Fact, Insight, Ranking, RankEntry, Source,
    )
    b = KnowledgeBundle(
        question="Сравнить комиссии и ставки Сбербанка и ВТБ по премиальным картам",
        intent="сравнение тарифов",
        subjects=["sberbank", "vtb"],
        subject_labels={"sberbank": "Сбербанк", "vtb": "ВТБ"},
    )
    # 3 источника
    b.sources.add(Source(url="https://sberbank.ru/cards", title="Сбер — тарифы",
                          domain="sberbank.ru", trust=0.95, kind="bank_official"))
    b.sources.add(Source(url="https://vtb.ru/cards", title="ВТБ — тарифы",
                          domain="vtb.ru", trust=0.95, kind="bank_official"))
    b.sources.add(Source(url="https://banki.ru/compare", title="Banki.ru сводка",
                          domain="banki.ru", trust=0.7, kind="aggregator"))
    # Факты: 3 различающихся числовых атрибута + 1 текстовый (не чартится)
    b.add_fact(Fact(subject="sberbank", attribute="годовая_комиссия",
                      value="0 ₽", source_n=1, confidence=0.9))
    b.add_fact(Fact(subject="vtb", attribute="годовая_комиссия",
                      value="4900 ₽", source_n=2, confidence=0.9))
    b.add_fact(Fact(subject="sberbank", attribute="ставка_кредитования",
                      value="15,9 %", source_n=1, confidence=0.85))
    b.add_fact(Fact(subject="vtb", attribute="ставка_кредитования",
                      value="14,5 %", source_n=2, confidence=0.85))
    b.add_fact(Fact(subject="sberbank", attribute="лимит_снятия",
                      value="300 000 ₽/мес", source_n=1, confidence=0.8))
    b.add_fact(Fact(subject="vtb", attribute="лимит_снятия",
                      value="500 000 ₽/мес", source_n=2, confidence=0.8))
    # Текстовый — в chart попасть не должен
    b.add_fact(Fact(subject="sberbank", attribute="цвет_карты",
                      value="чёрный", source_n=1, confidence=0.5))
    # Рейтинг + инсайт — для виджетов
    b.ranking = Ranking(criterion="по совокупности цена+гибкость")
    b.ranking.entries = [
        RankEntry(subject="sberbank", rank=1, score=8.5,
                    rationale="Бесплатное обслуживание, выше ставка кредита",
                    evidence_ns=[1]),
        RankEntry(subject="vtb", rank=2, score=7.0,
                    rationale="Платное обслуживание, но выгоднее ставка",
                    evidence_ns=[2]),
    ]
    b.insights = [
        Insight(headline="Ставки различаются на 1,4 п.п.",
                  explanation="Сбер дороже в кредитовании, но бесплатен в обслуживании.",
                  evidence_ns=[1, 2], impact="Сравнение зависит от профиля клиента"),
    ]
    return b


# ─── Mock OpenAI-клиента ──────────────────────────────────────────────────

class _Choice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, captured):
        self._captured = captured  # dict, куда пишем последний user_msg

    async def create(self, *, model, messages, **kw):
        # запоминаем user-промпт для проверок дедупа
        for m in messages:
            if m["role"] == "user":
                self._captured["last_user"] = m["content"]
        # возвращаем минимальный валидный отчёт
        return _Resp("## TL;DR\n\nСбер бесплатный, ВТБ — 4900 ₽/год [2].")


class _MockClient:
    def __init__(self):
        self.captured = {}
        self.chat = type("C", (), {})()
        self.chat.completions = _Completions(self.captured)


# ─── Проверки ─────────────────────────────────────────────────────────────

def _check(label, cond, detail=""):
    mark = "✅" if cond else "❌"
    print(f"  {mark} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _check.failed += 1
_check.failed = 0


def test_chart_specs():
    print("\n[1] extract_chart_specs()")
    b = _make_bundle()
    specs = b.extract_chart_specs()
    _check("вернул список specs", isinstance(specs, list),
             f"получено {type(specs)}")
    _check("≥1 chart (есть различающиеся числовые атрибуты)",
             len(specs) >= 1, f"получено {len(specs)} charts")
    if specs:
        s = specs[0]
        _check("chartType='bar'", s.get("chartType") == "bar",
                 f"получено {s.get('chartType')}")
        _check("labels = оба субъекта", set(s.get("labels", [])) == {"Сбербанк", "ВТБ"},
                 f"получено {s.get('labels')}")
        ds = (s.get("datasets") or [{}])[0]
        _check("dataset.data — 2 числа", len(ds.get("data", [])) == 2,
                 f"получено {ds.get('data')}")
        _check("sourceCitations непустой", bool(s.get("sourceCitations")),
                 f"получено {s.get('sourceCitations')}")
    # цвет_карты (текстовый) не должен попасть в чарты
    chart_titles = " ".join(s.get("title", "") for s in specs).lower()
    _check("текстовый атрибут (цвет_карты) исключён", "цвет" not in chart_titles,
             f"titles={chart_titles}")


def test_comparison_table():
    print("\n[2] to_comparison_table()")
    b = _make_bundle()
    md = b.to_comparison_table()
    _check("вернул markdown", isinstance(md, str) and len(md) > 50)
    _check("содержит шапку таблицы (|)", "| Параметр |" in md or "|" in md)
    _check("содержит цитату [N]", "[" in md)
    _check("не содержит текстовый атрибут 'цвет'", "цвет" not in md.lower())


async def test_write_report_preview():
    print("\n[3] write_report(preview_emitted=True/False)")
    from bank_audit.research.v2.analyst import write_report
    from bank_audit.research.v2.conductor import ResearchPlan

    plan = ResearchPlan(intent="сравнение", intent_summary="сводное сравнение",
                          question_nature="tariff_product",
                          subjects=["sberbank", "vtb"], subject_labels={},
                          product="премиальные карты",
                          missions=[], dependencies={}, output_sections=["key_findings"])

    # preview_emitted=True → таблица НЕ в промпте, есть напоминание НЕ дублировать
    client_t = _MockClient()
    b = _make_bundle()
    await write_report(client_t, b, plan, preview_emitted=True)
    user_t = client_t.captured.get("last_user", "")
    _check("preview=True: НЕТ блока 'ТАБЛИЦА СРАВНЕНИЯ (готовая)'",
             "ТАБЛИЦА СРАВНЕНИЯ (готовая" not in user_t)
    _check("preview=True: ЕСТЬ напоминание 'уже отрисована'",
             "уже отрисована" in user_t.lower(),
             "ожидали фразу про уже отрисованную таблицу")

    # preview_emitted=False → таблица в промпте как раньше
    client_f = _MockClient()
    b2 = _make_bundle()
    await write_report(client_f, b2, plan, preview_emitted=False)
    user_f = client_f.captured.get("last_user", "")
    _check("preview=False: ЕСТЬ блок 'ТАБЛИЦА СРАВНЕНИЯ (готовая)'",
             "ТАБЛИЦА СРАВНЕНИЯ (готовая" in user_f)


def test_imports():
    print("\n[4] импорты модулей")
    errors = []
    for mod in [
        "bank_audit.research.v2.knowledge_bundle",
        "bank_audit.research.v2.analyst",
        "bank_audit.research.v2.orchestrator",
    ]:
        try:
            __import__(mod)
        except Exception as e:
            errors.append(f"{mod}: {e}")
    _check("все модули импортируются", not errors, "; ".join(errors))
    # сигнатуры
    import inspect
    from bank_audit.research.v2 import analyst, orchestrator
    wr_params = inspect.signature(analyst.write_report).parameters
    _check("write_report(preview_emitted) есть",
             "preview_emitted" in wr_params)
    rw_params = inspect.signature(orchestrator._rewrite_with_critique).parameters
    _check("_rewrite_with_critique(preview_emitted) есть",
             "preview_emitted" in rw_params)
    from bank_audit.research.v2.knowledge_bundle import KnowledgeBundle
    _check("KnowledgeBundle.extract_chart_specs есть",
             hasattr(KnowledgeBundle, "extract_chart_specs"))


def test_frontend_handlers():
    print("\n[5] фронт handlers (app.jsx)")
    app = (ROOT / "src/bank_audit/web/static/app.jsx").read_text(encoding="utf-8")
    _check("dispatch: data.type===\"ranking\"", 'data.type==="ranking"' in app)
    _check("dispatch: data.type===\"insights\"", 'data.type==="insights"' in app)
    _check("виджет RankingWidget определён", "function RankingWidget" in app)
    _check("виджет InsightsWidget определён", "function InsightsWidget" in app)
    _check("виджеты подключены в рендер",
             "<RankingWidget" in app and "<InsightsWidget" in app)
    css = (ROOT / "src/bank_audit/web/static/index.html").read_text(encoding="utf-8")
    _check("CSS: .dr-ranking есть", ".dr-ranking" in css)
    _check("CSS: .dr-insights есть", ".dr-insights" in css)


def main():
    test_imports()
    test_chart_specs()
    test_comparison_table()
    asyncio.run(test_write_report_preview())
    test_frontend_handlers()
    print()
    if _check.failed:
        print(f"❌ FAILED: {_check.failed} проверок не прошли")
        sys.exit(1)
    print("✅ ALL PASSED")


if __name__ == "__main__":
    main()
