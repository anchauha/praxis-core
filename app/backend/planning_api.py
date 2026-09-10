"""Local-browser planning sessions, source selection and persisted draft generation."""

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .config import get_settings
from .ollama_client import OllamaError, OllamaUnavailable
from .planning import build_prompt, check_draft, generation_record, prepare_context
from .retrieval import CorpusError, StandardsCatalog
from .schemas import LessonPlanDraft, PlanningContext, RevisionEvent
from .store import new_event_id

router = APIRouter(prefix="/api/planning", tags=["planning"])
COOKIE = "praxis_planning_owner"


def owner(request: Request, response: Response):
    # The opaque bearer token identifies this browser, not a teacher account.
    token = request.cookies.get(COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        token = secrets.token_urlsafe(32)
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=60 * 60 * 24 * 365)
    return token


def catalog():
    settings = get_settings()
    try:
        return StandardsCatalog(settings.standards_index, settings.course_manifest)
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(503, "The standards corpus is unavailable or inconsistent. Check the index and manifest.") from exc


def owned_session(request, session_id, owner_id):
    store = request.app.state.store
    if store.get_session(session_id, owner_id) is None:
        raise HTTPException(404, "Planning session not found in this browser.")
    return store


class NewSession(BaseModel):
    title: str = Field(default="New lesson", min_length=1, max_length=120)


class SaveContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: PlanningContext


class GenerateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_snapshot_id: str
    model: str = Field(min_length=1, max_length=200)


class EditDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: LessonPlanDraft
    reviewer_notes: str | None = Field(default=None, max_length=2000)


@router.get("/courses")
def courses():
    source = catalog()
    return {"courses": source.courses(), "archived_standards": len(source.index["standards"]),
            "available_standards": len(source.available)}


@router.get("/standards")
def search(course_key: str, q: str = Query(default="", max_length=300),
           limit: int = Query(default=40, ge=1, le=100)):
    try:
        return catalog().search(course_key, q, limit)
    except CorpusError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/sessions")
def sessions(request: Request, owner_id=Depends(owner)):
    return [{k: row[k] for k in ("session_id", "title", "created_at")}
            for row in request.app.state.store.list_sessions(owner_id)]


@router.post("/sessions", status_code=201)
def create_session(body: NewSession, request: Request, owner_id=Depends(owner)):
    session_id = request.app.state.store.create_session(owner_id, title=body.title)
    return {"session_id": session_id}


@router.get("/sessions/{session_id}")
def session_state(session_id: str, request: Request, owner_id=Depends(owner)):
    store = owned_session(request, session_id, owner_id)
    ctx = store.latest_context(session_id, owner_id)
    evidence = store.evidence_for_context(ctx.event_id, owner_id) if ctx else None
    generations = store.generations(session_id, owner_id)
    revisions = {g.event_id: store.revisions_for(g.event_id, owner_id) for g in generations}
    evidence_by_generation = {}
    for generation in generations:
        replayed = store.replay(generation.event_id, owner_id)
        if replayed:
            evidence_by_generation[generation.event_id] = replayed[1]
    return {"context_snapshot": ctx, "evidence_snapshot": evidence,
            "generations": generations, "revisions": revisions,
            "evidence_by_generation": evidence_by_generation}


@router.post("/sessions/{session_id}/contexts", status_code=201)
def save_context(session_id: str, body: SaveContext, request: Request, owner_id=Depends(owner)):
    store = owned_session(request, session_id, owner_id)
    try:
        ctx, evd = prepare_context(body.context, session_id, catalog())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    store.append_evidence_snapshot(evd, owner_id)
    store.append_context_snapshot(ctx, owner_id)
    return {"context_snapshot": ctx, "evidence_snapshot": evd}


@router.post("/sessions/{session_id}/generations", status_code=201)
async def generate(session_id: str, body: GenerateDraft, request: Request, owner_id=Depends(owner)):
    store = await run_in_threadpool(owned_session, request, session_id, owner_id)
    ctx = await run_in_threadpool(store.get_context_snapshot, body.context_snapshot_id, owner_id)
    evd = await run_in_threadpool(store.evidence_for_context, body.context_snapshot_id, owner_id)
    if ctx is None or evd is None or ctx.session_id != session_id or evd.session_id != session_id:
        raise HTTPException(404, "Saved context and evidence not found. Save the planning context first.")
    try:
        source = await run_in_threadpool(catalog)
        source.check_snapshot(evd.bundle)
        settings = get_settings()
        messages, byte_count = build_prompt(ctx.context, evd.bundle, num_ctx=settings.num_ctx,
                                            num_predict=settings.planning_num_predict)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    client = request.app.state.ollama
    try:
        installed = await client.list_models()
    except OllamaUnavailable as exc:
        raise HTTPException(503, "Ollama is unavailable. The saved context is still available.") from exc
    selected = next((model for model in installed if model["name"] == body.model), None)
    if selected is None or selected["is_cloud"]:
        raise HTTPException(422, "Select an installed local model. This planner requires local structured outputs.")
    payload = {"model": body.model, "messages": messages, "stream": False, "think": False,
               "format": LessonPlanDraft.model_json_schema(),
               "options": {"num_ctx": settings.num_ctx, "num_predict": settings.planning_num_predict,
                           "temperature": 0.2}}
    options = {"request": payload, "model_digest": selected.get("digest"),
               "prompt_utf8_bytes": byte_count, "prompt_budget_method": "utf8_bytes_plus_512_template_reserve"}
    output, failure, truncated = "", None, False
    try:
        result = await client.complete(payload)
        message = result.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            failure = "Ollama returned no text content."
        else:
            output = message["content"]
        truncated = result.get("done_reason") == "length"
        options["response_metadata"] = {k: result[k] for k in (
            "model", "done", "done_reason", "prompt_eval_count", "eval_count", "total_duration") if k in result}
        if result.get("done") is not True:
            failure = "Ollama did not return a completed response."
    except (OllamaError, OllamaUnavailable) as exc:
        failure = str(exc)
    event = generation_record(ctx, evd, body.model, messages, options, output,
                              error=failure, truncated=truncated)
    await run_in_threadpool(store.append_generation, event, owner_id)
    return event


@router.post("/sessions/{session_id}/generations/{generation_id}/revisions", status_code=201)
def revise(session_id: str, generation_id: str, body: EditDraft, request: Request, owner_id=Depends(owner)):
    store = owned_session(request, session_id, owner_id)
    replayed = store.replay(generation_id, owner_id)
    if replayed is None or replayed[2].session_id != session_id:
        raise HTTPException(404, "Generation not found.")
    ctx, evd, _ = replayed
    plan, report = check_draft(body.draft, ctx.context, evd.bundle)
    event = RevisionEvent(event_id=new_event_id("rev"), generation_id=generation_id,
                          edited_plan=plan, review_status="developer_edit_unreviewed",
                          reviewer_notes=body.reviewer_notes)
    store.append_revision(event, session_id, owner_id)
    return event
