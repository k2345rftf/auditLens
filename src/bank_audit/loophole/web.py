"""FastAPI APIRouter модуля loophole: эндпоинты + SSE-чат.

Префикс /api/loophole (монтируется в web/app.py). Авторизация — RBAC через
.auth: get_current_user + require_action(action) идёт по policy dict[role, action].
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .. import db
from . import collector as collector_mod
from . import logging_audit
from . import refine as refine_mod
from . import repository as repo
from . import workspace as ws_mod
from . import auth as auth_mod
from .auth import (
    ACT_READ_LOOPHOLES,
    ACT_CHANGE_STATUS,
    ACT_CREATE_PARSER,
    ACT_RUN_PARSER,
    ACT_DELETE_PARSER,
    UserPrincipal,
    get_session,
    require_action,
)
from .chat import graph as chat_graph
from .chat.state import ChatState
from .kb import repository as kb_repo
from .models import ExportRequest, SearchQuery, WorkspaceCreate

log = logging.getLogger(__name__)

router = APIRouter()

# Допустимые значения loophole_record.status — для ручного изменения через API.
# Автоклассификация использует update_verdict (см. repository.py).
VALID_STATUSES: frozenset[str] = frozenset(
    {"new", "classified", "in_review", "fixed", "false_positive", "archived"}
)


def _csv_value(value: Any) -> Any:
    """Заменяет переводы строк в значениях CSV на пробелы."""
    if isinstance(value, str):
        return value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return value


# ── Dependencies ────────────────────────────────────────────────────────────
# get_session импортируется из .auth (единое место для override'а тестами).
# get_current_user / require_action — там же (см. POLICY и resolve_role).


# ── Эндпоинты ───────────────────────────────────────────────────────────────
@router.post("/search")
def search(
    q: SearchQuery,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    records = repo.search_relevant(
        q.query_text,
        bank_slugs=q.bank_slugs or None,
        period_from=q.period_from,
        period_to=q.period_to,
        only_loophole=True,
        session=session,
    )
    logging_audit.log_action(
        user.user_id, "search",
        detail={"query": q.query_text, "banks": q.bank_slugs},
        session=session,
    )
    return {"records": records, "count": len(records)}


@router.get("/keywords")
def get_keywords(
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    return {"keywords": repo.list_keywords(session=session)}


@router.get("/records")
def list_records(
    bank_slugs: str | None = None,
    period_from: date | None = None,
    period_to: date | None = None,
    q: str | None = None,
    only_loophole: bool | None = None,
    status: str | None = None,
    limit: int = 500,
    offset: int = 0,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    """Список лазеек из БД для таблицы в основной области UI."""
    slugs = (
        [s.strip() for s in bank_slugs.split(",") if s.strip()]
        if bank_slugs else None
    )
    records = repo.list_records(
        bank_slugs=slugs,
        period_from=period_from,
        period_to=period_to,
        query_text=q,
        only_loophole=only_loophole,
        status=status,
        limit=limit,
        offset=offset,
        session=session,
    )
    return {"records": records, "count": len(records)}


@router.get("/records/{record_id}/content")
def record_content(
    record_id: int,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    """Полный контент записи — ленивая подгрузка для разворачивающейся строки UI."""
    record = repo.get_record(record_id, session=session)
    if record is None:
        raise HTTPException(status_code=404, detail="record not found")
    logging_audit.log_action(
        user.user_id, "view_content",
        detail={"record_id": record_id}, session=session,
    )
    return {
        "record_id": record_id,
        "content_status": record.get("content_status"),
        "raw_text": record.get("raw_text"),
        "raw_text_len": record.get("raw_text_len"),
        "raw_text_truncated": bool(record.get("raw_text_truncated")),
        "fetched_at": record.get("fetched_at"),
    }


class BackfillRequest(BaseModel):
    limit: int = 100
    delay_ms: int = 500


@router.post("/records/backfill-content")
def backfill_content(
    body: BackfillRequest,
    user: UserPrincipal = Depends(require_action(ACT_CHANGE_STATUS)),
    session=Depends(get_session),
):
    """Догрузка полного контента для legacy/fetch_failed/empty записей."""
    import time

    from . import content_fetch

    targets = repo.list_records_needing_content(limit=body.limit, session=session)
    updated = 0
    failed = 0
    for i, row in enumerate(targets):
        if i:
            time.sleep(body.delay_ms / 1000)
        content = content_fetch.fetch_full_content(row["url"])
        if content.status == content_fetch.STATUS_FAILED:
            failed += 1
        else:
            updated += 1
        repo.update_content(
            row["record_id"],
            raw_text=content.text,
            content_status=content.status,
            raw_text_len=content.length,
            truncated=content.truncated,
            session=session,
        )
    remaining = repo.count_records_needing_content(session=session)
    logging_audit.log_action(
        user.user_id, "backfill_content",
        detail={"processed": len(targets), "updated": updated,
                "failed": failed, "remaining": remaining},
        session=session,
    )
    return {"processed": len(targets), "updated": updated,
            "failed": failed, "remaining": remaining}


class VerdictRequest(BaseModel):
    record_ids: list[int]
    is_loophole: bool
    comment: str | None = None


@router.post("/records/verdict")
def mark_verdict(
    body: VerdictRequest,
    user: UserPrincipal = Depends(require_action(ACT_CHANGE_STATUS)),
    session=Depends(get_session),
):
    """Ручная маркировка записей: «лазейка» / «обычный запрос» (admin/cko)."""
    if not body.record_ids:
        raise HTTPException(status_code=400, detail="record_ids пуст")
    updated: list[int] = []
    skipped: list[int] = []
    reason = body.comment or f"manual:{user.user_id}"
    for rid in body.record_ids:
        record = repo.get_record(rid, session=session)
        if record is None:
            skipped.append(rid)
            continue
        repo.update_verdict(
            rid,
            is_loophole=body.is_loophole,
            confidence=1.0,
            reason=reason,
            model="manual",
            session=session,
        )
        if body.is_loophole:
            if repo.get_kb_example_by_record(rid, session=session) is None:
                description = (
                    record.get("snippet")
                    or (record.get("raw_text") or "")[:2000]
                    or record.get("title")
                    or ""
                )
                kb_repo.add_example(
                    record.get("title") or description[:200],
                    description,
                    category="manual",
                    record_id=rid,
                    session=session,
                )
        else:
            repo.delete_kb_example_by_record(rid, session=session)
        updated.append(rid)
    logging_audit.log_action(
        user.user_id, "mark_verdict",
        detail={
            "ids": body.record_ids,
            "is_loophole": body.is_loophole,
            "comment": body.comment,
        },
        session=session,
    )
    return {"updated": len(updated), "skipped": skipped}


class StatusChangeRequest(BaseModel):
    status: str


@router.post("/records/{record_id}/status")
def change_status(
    record_id: int,
    body: StatusChangeRequest,
    user: UserPrincipal = Depends(require_action(ACT_CHANGE_STATUS)),
    session=Depends(get_session),
):
    """Ручная смена status лазейки (admin/cko). Автоклассификация —
    через update_verdict (см. repository.py); здесь — только ручной override."""
    if body.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status должен быть одним из {sorted(VALID_STATUSES)}",
        )
    existing = repo.get_record(record_id, session=session)
    if existing is None:
        raise HTTPException(status_code=404, detail="record not found")
    repo.update_record_status(record_id, body.status, session=session)
    logging_audit.log_action(
        user.user_id, "change_status",
        detail={
            "record_id": record_id,
            "new_status": body.status,
            "old_status": existing.get("status"),
        },
        session=session,
    )
    return {"record_id": record_id, "status": body.status}


@router.get("/banks")
def list_banks(
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    """Уникальные bank_slug из loophole_record — для фильтра таблицы."""
    return {"banks": repo.list_bank_slugs(session=session)}


@router.get("/workspaces")
def list_workspaces(
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    return {"workspaces": ws_mod.list_for_user(user.user_id, session=session)}


@router.post("/workspace")
def create_workspace(
    body: WorkspaceCreate,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    wid = ws_mod.create(user.user_id, name=body.name, session=session)
    logging_audit.log_action(
        user.user_id, "workspace_create", workspace_id=wid, session=session
    )
    return {"workspace_id": wid}


@router.get("/history/{workspace_id}")
def history(
    workspace_id: int,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    return {"messages": ws_mod.history(workspace_id, session=session)}


class ChatRequest(BaseModel):
    workspace_id: int
    message: str
    history: list[dict] = []
    skip_clarify: bool = False


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    """SSE-чат: стримит token/tool_call/tool_result/record события."""
    state: ChatState = {
        "query": body.message,
        "messages": body.history,
        "workspace_id": body.workspace_id,
        "user_id": user.user_id,
        "session": session,
        "skip_clarify": body.skip_clarify,
    }
    repo.add_chat_message(body.workspace_id, "user", body.message, session=session)
    logging_audit.log_action(
        user.user_id, "chat", workspace_id=body.workspace_id,
        detail={"message": body.message[:200]}, session=session,
    )

    async def event_generator():
        import json as _json
        async for ev in chat_graph.stream_chat(state, session=session):
            yield {"event": ev["event"], "data": _json.dumps(ev["data"], ensure_ascii=False, default=str)}
        try:
            if state.get("answer"):
                repo.add_chat_message(
                    body.workspace_id, "assistant", state["answer"], session=session
                )
        except Exception:
            pass

    return EventSourceResponse(event_generator())


EXPORT_LIMIT = 10000


@router.post("/export")
def export(
    body: ExportRequest,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    if body.records and len(body.records) > EXPORT_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Нельзя выгрузить более {EXPORT_LIMIT} записей за раз "
                f"(запрошено {len(body.records)}). Сократите выделение."
            ),
        )
    records = []
    if body.records:
        for rid in body.records:
            r = repo.get_record(rid, session=session)
            if r:
                records.append(r)
    logging_audit.log_action(
        user.user_id, "export", detail={"format": body.format, "count": len(records)},
        session=session,
    )
    if body.format == "json":
        return JSONResponse(records)
    if body.format == "csv":
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow([
            "record_id", "title", "url", "domain", "bank_slug", "keyword",
            "trust_score", "is_loophole", "verdict_confidence",
            "verdict_reason", "verdict_model", "status",
            "collected_at", "classified_at",
            "content_status", "raw_text_len", "raw_text",
        ])
        for r in records:
            writer.writerow([
                _csv_value(r.get("record_id")), _csv_value(r.get("title")),
                _csv_value(r.get("url")), _csv_value(r.get("domain")),
                _csv_value(r.get("bank_slug")), _csv_value(r.get("keyword")),
                _csv_value(r.get("trust_score")), _csv_value(r.get("is_loophole")),
                _csv_value(r.get("verdict_confidence")),
                _csv_value(r.get("verdict_reason")),
                _csv_value(r.get("verdict_model")), _csv_value(r.get("status")),
                _csv_value(r.get("collected_at")), _csv_value(r.get("classified_at")),
                _csv_value(r.get("content_status")), _csv_value(r.get("raw_text_len")),
                _csv_value(r.get("raw_text")),
            ])
        return Response(
            content="\ufeff" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=loopholes.csv"},
        )
    return JSONResponse({"error": "pdf export requires Playwright"}, status_code=501)


class FilteredExportRequest(BaseModel):
    bank_slugs: list[str] = Field(default_factory=list)
    period_from: date | None = None
    period_to: date | None = None
    query_text: str = ""
    only_loophole: bool | None = None
    status: str | None = None


@router.post("/export/csv")
def export_csv_filtered(
    body: FilteredExportRequest,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    """Выгрузка CSV по текущим фильтрам таблицы (без передачи ids)."""
    records = repo.list_records(
        bank_slugs=body.bank_slugs or None,
        period_from=body.period_from,
        period_to=body.period_to,
        query_text=body.query_text or None,
        only_loophole=body.only_loophole,
        status=body.status,
        limit=10000,
        include_content=True,
        session=session,
    )
    logging_audit.log_action(
        user.user_id, "export_csv", detail={"count": len(records)}, session=session,
    )
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow([
        "record_id", "title", "url", "domain", "bank_slug", "keyword",
        "trust_score", "is_loophole", "verdict_confidence",
        "verdict_reason", "verdict_model", "status",
        "collected_at", "classified_at",
        "content_status", "raw_text_len", "raw_text",
    ])
    for r in records:
        writer.writerow([
            _csv_value(r.get("record_id")), _csv_value(r.get("title")),
            _csv_value(r.get("url")), _csv_value(r.get("domain")),
            _csv_value(r.get("bank_slug")), _csv_value(r.get("keyword")),
            _csv_value(r.get("trust_score")), _csv_value(r.get("is_loophole")),
            _csv_value(r.get("verdict_confidence")),
            _csv_value(r.get("verdict_reason")),
            _csv_value(r.get("verdict_model")), _csv_value(r.get("status")),
            _csv_value(r.get("collected_at")), _csv_value(r.get("classified_at")),
            _csv_value(r.get("content_status")), _csv_value(r.get("raw_text_len")),
            _csv_value(r.get("raw_text")),
        ])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=loopholes.csv"},
    )


# POST /refine требует ACT_CHANGE_STATUS (admin/cko): refine неявно модифицирует
# keyword-таблицу, что влияет на дальнейшую классификацию.
@router.post("/refine")
async def refine(
    user: UserPrincipal = Depends(require_action(ACT_CHANGE_STATUS)),
    session=Depends(get_session),
):
    added = await refine_mod.refine_keywords(session=session)
    logging_audit.log_action(
        user.user_id, "refine", detail={"added": added}, session=session
    )
    return {"added": added}


@router.post("/collect/run")
async def collect_run(
    user: UserPrincipal = Depends(require_action(ACT_CHANGE_STATUS)),
    session=Depends(get_session),
):
    """Ручной запуск авто-сборщика (admin/cko): создаёт записи в loophole_record."""
    n = await collector_mod.collect_once(session=session)
    logging_audit.log_action(
        user.user_id, "collect", detail={"new_records": n}, session=session
    )
    return {"new_records": n}


# ── Clarify-воронка ─────────────────────────────────────────────────────────
class ClarifyRequest(BaseModel):
    question: str
    history: list[dict] = Field(default_factory=list)


class ClarifyAnswerRequest(BaseModel):
    question: str
    answers: list[dict] = Field(default_factory=list)


@router.post("/clarify")
async def clarify(
    body: ClarifyRequest,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    """Генерация уточняющих вопросов по запросу аудитора."""
    from .chat import clarify as clarify_mod

    result = await clarify_mod.generate_clarifications(
        body.question, history=body.history
    )
    logging_audit.log_action(
        user.user_id, "clarify",
        detail={"question": body.question[:200], "complete": result.get("complete")},
        session=session,
    )
    return result


@router.post("/clarify/answer")
async def clarify_answer(
    body: ClarifyAnswerRequest,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    """Сборка обогащённого запроса из исходного вопроса и ответов воронки."""
    from .chat import clarify as clarify_mod

    enriched = await clarify_mod.build_enriched_question(body.question, body.answers)
    logging_audit.log_action(
        user.user_id, "clarify_answer",
        detail={"question": body.question[:200], "enriched_len": len(enriched)},
        session=session,
    )
    return {"enriched_question": enriched}


# ── Парсеры: общий каталог ──────────────────────────────────────────────────
class ParserCreateRequest(BaseModel):
    workspace_id: int
    query: str


class ParserPatchRequest(BaseModel):
    name: str | None = None
    cron_expr: str | None = None
    auto_enabled: bool | None = None


@router.get("/parsers")
def list_parsers(
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    """Общий каталог парсеров всех пользователей + статистика карточек."""
    from .parsers import registry as parser_registry

    return {"parsers": parser_registry.list_catalog(session=session)}


@router.post("/parsers")
async def create_parser(
    body: ParserCreateRequest,
    user: UserPrincipal = Depends(require_action(ACT_CREATE_PARSER)),
    session=Depends(get_session),
):
    """Создание парсера: дедуп источников (409 при полном дубле) + LLM-генерация."""
    from .parsers import dedup as dedup_mod
    from .parsers import generator as parser_generator
    from .parsers import registry as parser_registry

    targets = parser_generator.extract_targets(body.query)
    if not targets:
        raise HTTPException(
            status_code=422,
            detail="В запросе не указан URL ресурса или группа мессенджера",
        )
    keys = [k for k in (dedup_mod.normalize_target(t) for t in targets) if k]
    conflicts = parser_registry.find_conflicts(keys, session=session)
    full = [c for c in conflicts if sorted(c["overlap"]) == sorted(keys)]
    if full:
        c = full[0]
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate",
                "conflict_with": {"parser_id": c["parser_id"], "name": c["name"]},
            },
        )
    try:
        result = await parser_generator.generate_parser(
            user.user_id, body.workspace_id, body.query, session=None
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if conflicts:
        result["warnings"] = [
            {"target": o, "conflict_with": c["parser_id"]}
            for c in conflicts for o in c["overlap"]
        ]
    logging_audit.log_action(
        user.user_id, "parser_create",
        workspace_id=body.workspace_id,
        detail={"parser_id": result.get("parser_id"), "query": body.query[:200]},
        session=session,
    )
    return {
        "parser_id": result["parser_id"],
        "validation_run_id": result["validation_run_id"],
        "name": result["name"],
        "targets": result["targets"],
        "warnings": result.get("warnings") or [],
    }


@router.patch("/parsers/{parser_id}")
def patch_parser(
    parser_id: int,
    body: ParserPatchRequest,
    user: UserPrincipal = Depends(require_action(ACT_CREATE_PARSER)),
    session=Depends(get_session),
):
    """Редактирование: имя, cron (валидация croniter), вкл/выкл автозапуска."""
    from .parsers import scheduler as parser_scheduler

    row = repo.get_parser(parser_id, session=session)
    if row is None:
        raise HTTPException(status_code=404, detail="parser not found")
    if body.cron_expr is not None:
        cron_expr = body.cron_expr or None
    else:
        cron_expr = row.get("cron_expr")
    auto_enabled = (
        body.auto_enabled
        if body.auto_enabled is not None
        else bool(row.get("auto_enabled"))
    )
    nxt = None
    if cron_expr:
        try:
            nxt = parser_scheduler.next_run(cron_expr)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    if not auto_enabled:
        nxt = None
    repo.update_parser_schedule(
        parser_id,
        cron_expr=cron_expr,
        auto_enabled=auto_enabled,
        next_run_at=nxt,
        last_edited_by=user.user_id,
        name=body.name,
        session=session,
    )
    logging_audit.log_action(
        user.user_id, "parser_edit",
        detail={"parser_id": parser_id, "cron": cron_expr, "auto": auto_enabled},
        session=session,
    )
    updated = repo.get_parser(parser_id, session=session)
    updated["next_run_at"] = nxt.isoformat() if nxt else None
    return {"parser": updated}


@router.post("/parsers/{parser_id}/run")
async def run_parser(
    parser_id: int,
    user: UserPrincipal = Depends(require_action(ACT_RUN_PARSER)),
    session=Depends(get_session),
):
    """Ручной запуск парсера. Возвращает run_id для SSE-подписки на лог."""
    from .parsers import runner as runner_mod

    try:
        run_id = await runner_mod.run(parser_id, "manual")
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    logging_audit.log_action(
        user.user_id, "parser_run",
        detail={"parser_id": parser_id, "run_id": run_id},
        session=session,
    )
    return {"parser_id": parser_id, "run_id": run_id}


@router.post("/parsers/{parser_id}/stop")
async def stop_parser(
    parser_id: int,
    user: UserPrincipal = Depends(require_action(ACT_RUN_PARSER)),
    session=Depends(get_session),
):
    """Останов запущенного парсера. 404 если не running."""
    from .parsers.runner import _RUNNING

    runner = _RUNNING.get(parser_id)
    if runner is None:
        raise HTTPException(status_code=404, detail="parser not running")
    await runner.stop()
    logging_audit.log_action(
        user.user_id, "parser_stop", detail={"parser_id": parser_id}, session=session,
    )
    return {"parser_id": parser_id, "stopped": True}


@router.get("/parsers/{parser_id}/status")
async def parser_status(
    parser_id: int,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    """Статус парсера: runtime (если running) + запись из БД."""
    from .parsers import registry as parser_registry
    from .parsers.runner import _RUNNING

    runner = _RUNNING.get(parser_id)
    if runner is not None:
        runtime = await runner.status()
    else:
        runtime = None
    row = parser_registry.get_parser(parser_id, session=session)
    if row is None and runtime is None:
        raise HTTPException(status_code=404, detail="parser not found")
    return {"parser_id": parser_id, "runtime": runtime, "parser": row}


@router.get("/parsers/{parser_id}/runs")
def list_parser_runs(
    parser_id: int,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    """История запусков (последние 20)."""
    return {"runs": repo.list_runs(parser_id, session=session)}


@router.get("/parsers/{parser_id}/log/stream")
async def parser_log_stream(
    parser_id: int,
    run_id: int,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
):
    """SSE-стрим лога запуска."""
    from .parsers import runner as runner_mod

    queue = runner_mod.subscribe(run_id)

    async def events():
        try:
            for line in runner_mod.log_tail(run_id):
                yield {"event": "log", "data": line}
            while True:
                msg = await queue.get()
                yield msg
                if msg.get("event") == "done":
                    break
        finally:
            runner_mod.unsubscribe(run_id, queue)

    return EventSourceResponse(events())


@router.post("/parsers/{parser_id}/heal")
async def heal_parser(
    parser_id: int,
    user: UserPrincipal = Depends(require_action(ACT_CREATE_PARSER)),
    session=Depends(get_session),
):
    """Ручной запуск анализа и восстановления парсера nanobot'ом."""
    from .parsers import healer as healer_mod

    if not healer_mod.nanobot_available():
        raise HTTPException(status_code=503, detail="nanobot unavailable")
    try:
        run_id = await healer_mod.heal(parser_id, manual=True, session=session)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    logging_audit.log_action(
        user.user_id, "parser_heal",
        detail={"parser_id": parser_id, "run_id": run_id},
        session=session,
    )
    return {"parser_id": parser_id, "heal_run_id": run_id}


@router.delete("/parsers/{parser_id}")
def delete_parser(
    parser_id: int,
    user: UserPrincipal = Depends(require_action(ACT_DELETE_PARSER)),
    session=Depends(get_session),
):
    """Удаление парсера (admin only). 409 если running, 404 если не найден."""
    from .parsers import registry as parser_registry
    from .parsers.runner import _RUNNING

    if parser_id in _RUNNING:
        raise HTTPException(status_code=409, detail="parser running")
    deleted = parser_registry.delete_parser(parser_id, session=session)
    if not deleted:
        raise HTTPException(status_code=404, detail="parser not found")
    logging_audit.log_action(
        user.user_id, "parser_delete", detail={"parser_id": parser_id}, session=session,
    )
    return {"deleted": True}


# ── Загрузка таблицы по фильтрам (для агента) ───────────────────────────────
class TableLoadRequest(BaseModel):
    bank_slugs: list[str] = Field(default_factory=list)
    period_from: date | None = None
    period_to: date | None = None
    query_text: str = ""
    only_loophole: bool | None = None
    status: str | None = None
    limit: int = 500
    offset: int = 0


@router.post("/table/load")
def table_load(
    body: TableLoadRequest,
    user: UserPrincipal = Depends(require_action(ACT_READ_LOOPHOLES)),
    session=Depends(get_session),
):
    """Применяет фильтры и возвращает records для таблицы UI."""
    records = repo.list_records(
        bank_slugs=body.bank_slugs or None,
        period_from=body.period_from,
        period_to=body.period_to,
        query_text=body.query_text or None,
        only_loophole=body.only_loophole,
        status=body.status,
        limit=body.limit,
        offset=body.offset,
        session=session,
    )
    return {"records": records, "count": len(records)}


# ── Подключение admin-роутера (CRUD маппинга групп/ролей) ──────────────────
from .auth_admin import router as auth_admin_router
router.include_router(auth_admin_router)
