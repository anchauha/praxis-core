"""SQLite store for sessions, planning events and dataset exports.

Events are written once and never edited. A generation records the exact context
and evidence it was given, so a training example can be rebuilt later without
consulting a corpus that has moved on since. An UPDATE on the events table is
refused by a trigger.

Withdrawal is the deliberate exception. A contributor may take their material
back, so ``redact_session`` removes a whole session and leaves a tombstone
saying it happened. Note the limit honestly: removing a row stops it reaching
future exports and retrieval, but it cannot remove whatever a model has already
learnt from it.

Session and event reads are scoped by ``owner_id``. Asking for another owner's
session returns nothing, so the store does not confirm that a row exists.

The methods are synchronous. SQLite writes here are small, but call them from
async paths through ``starlette.concurrency.run_in_threadpool`` rather than
blocking the event loop.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schemas import (
    ContextSnapshot,
    EvidenceSnapshot,
    ExportManifest,
    GenerationEvent,
    LabelOrigin,
    PreferenceEvent,
    PreferenceLabel,
    RevisionEvent,
    Severity,
    TrainingUseStatus,
)

SCHEMA_USER_VERSION = 1

EVENT_KINDS = (
    "context_snapshot",
    "evidence_snapshot",
    "generation",
    "revision",
    "preference",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id              TEXT PRIMARY KEY,
    owner_id                TEXT NOT NULL,
    -- Related cases share a family so a split can be assigned above the level
    -- of a single session. A paraphrase must not land on the other side of it.
    family_id               TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    title                   TEXT,
    -- Four consents, kept apart on purpose. Agreeing to one is not agreeing
    -- to the next.
    consent_classroom_use   INTEGER NOT NULL DEFAULT 1,
    consent_research        INTEGER NOT NULL DEFAULT 0,
    consent_model_training  INTEGER NOT NULL DEFAULT 0,
    consent_public_sharing  INTEGER NOT NULL DEFAULT 0,
    closed_at               TEXT
);

CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(owner_id, created_at);
CREATE INDEX IF NOT EXISTS sessions_family ON sessions(family_id);

CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,
    session_id          TEXT NOT NULL REFERENCES sessions(session_id),
    family_id           TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    schema_version      TEXT NOT NULL,
    -- Lifted out of the payload because reproducing a generation depends on
    -- following them.
    context_snapshot_id TEXT,
    evidence_snapshot_id TEXT,
    generation_id       TEXT,
    payload             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_session ON events(session_id, kind, created_at);
CREATE INDEX IF NOT EXISTS events_family ON events(family_id, kind);
CREATE INDEX IF NOT EXISTS events_generation ON events(generation_id);

-- An event is a record of what happened. Correcting it would rewrite history
-- and quietly invalidate every export already drawn from it.
CREATE TRIGGER IF NOT EXISTS events_are_immutable
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are immutable; append a new event instead');
END;

CREATE TABLE IF NOT EXISTS splits (
    family_id   TEXT PRIMARY KEY,
    split       TEXT NOT NULL CHECK (split IN ('train', 'dev', 'test')),
    assigned_at TEXT NOT NULL,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS export_manifests (
    export_id       TEXT PRIMARY KEY,
    dataset_version TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    payload         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS redactions (
    redaction_id   TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    family_id      TEXT NOT NULL,
    owner_id       TEXT NOT NULL,
    redacted_at    TEXT NOT NULL,
    events_removed INTEGER NOT NULL,
    reason         TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class SessionRow(dict):
    """A session row. A plain mapping keeps the store free of extra models."""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.initialise()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- setup ------------------------------------------------------------

    def initialise(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # -- sessions ---------------------------------------------------------

    def create_session(
        self,
        owner_id: str,
        *,
        family_id: str | None = None,
        title: str | None = None,
        consent_research: bool = False,
        consent_model_training: bool = False,
        consent_public_sharing: bool = False,
    ) -> str:
        """Start a session. It gets its own family unless one is named."""
        session_id = _new_id("ses")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions (session_id, owner_id, family_id, created_at,"
                " title, consent_research, consent_model_training,"
                " consent_public_sharing) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, owner_id, family_id or session_id, _now(), title,
                 int(consent_research), int(consent_model_training),
                 int(consent_public_sharing)),
            )
        return session_id

    def get_session(self, session_id: str, owner_id: str) -> SessionRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND owner_id = ?",
                (session_id, owner_id),
            ).fetchone()
        return SessionRow(row) if row else None

    def list_sessions(self, owner_id: str) -> list[SessionRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE owner_id = ? ORDER BY created_at DESC",
                (owner_id,),
            ).fetchall()
        return [SessionRow(r) for r in rows]

    def set_consent(self, session_id: str, owner_id: str, **flags: bool) -> bool:
        """Change consent. Withdrawing it here stops future exports, not past ones."""
        allowed = {"consent_classroom_use", "consent_research",
                   "consent_model_training", "consent_public_sharing"}
        unknown = set(flags) - allowed
        if unknown:
            raise ValueError(f"unknown consent flags: {sorted(unknown)}")
        if not flags:
            return False
        assignments = ", ".join(f"{name} = ?" for name in flags)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE sessions SET {assignments} WHERE session_id = ? AND owner_id = ?",
                (*(int(v) for v in flags.values()), session_id, owner_id),
            )
        return cur.rowcount > 0

    # -- appending events -------------------------------------------------

    def _append(self, kind: str, session_id: str, owner_id: str, model: Any,
                *, context_snapshot_id: str | None = None,
                evidence_snapshot_id: str | None = None,
                generation_id: str | None = None) -> str:
        session = self.get_session(session_id, owner_id)
        if session is None:
            raise PermissionError(
                f"session {session_id!r} is not available to owner {owner_id!r}")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events (event_id, kind, session_id, family_id,"
                " created_at, schema_version, context_snapshot_id,"
                " evidence_snapshot_id, generation_id, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (model.event_id, kind, session_id, session["family_id"],
                 model.created_at.isoformat(), model.schema_version,
                 context_snapshot_id, evidence_snapshot_id, generation_id,
                 model.model_dump_json()),
            )
        return model.event_id

    def append_context_snapshot(self, snapshot: ContextSnapshot, owner_id: str) -> str:
        """Record the planning state as it stands. The previous one is kept."""
        return self._append("context_snapshot", snapshot.session_id, owner_id, snapshot)

    def append_evidence_snapshot(self, snapshot: EvidenceSnapshot, owner_id: str) -> str:
        return self._append("evidence_snapshot", snapshot.session_id, owner_id, snapshot)

    def append_generation(self, event: GenerationEvent, owner_id: str) -> str:
        return self._append(
            "generation", event.session_id, owner_id, event,
            context_snapshot_id=event.context_snapshot_id,
            evidence_snapshot_id=event.evidence_snapshot_id)

    def append_revision(self, event: RevisionEvent, session_id: str, owner_id: str) -> str:
        return self._append("revision", session_id, owner_id, event,
                            generation_id=event.generation_id)

    def append_preference(self, event: PreferenceEvent, session_id: str,
                          owner_id: str) -> str:
        # Keep validation and insertion together, including across connections.
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            if self.get_session(session_id, owner_id) is None:
                raise PermissionError("session is not available to this owner")
            reasons = self._preference_input_errors(event, session_id, owner_id)
            if reasons:
                raise ValueError("invalid preference inputs: " + "; ".join(reasons))
            return self._append(
                "preference", session_id, owner_id, event,
                context_snapshot_id=event.context_snapshot_id,
                evidence_snapshot_id=event.evidence_snapshot_id)

    def _preference_input_errors(self, event: PreferenceEvent, session_id: str,
                                 owner_id: str) -> list[str]:
        """Shared by insertion and export checks, including legacy rows."""
        reasons: list[str] = []
        if event.candidate_a_generation_id == event.candidate_b_generation_id:
            reasons.append("candidates must be distinct generations")
        for event_id, kind in ((event.context_snapshot_id, "context_snapshot"),
                               (event.evidence_snapshot_id, "evidence_snapshot")):
            payload = self._payload(event_id, owner_id, kind)
            if payload is None or payload.get("session_id") != session_id:
                reasons.append(f"{kind} is missing or outside this owner/session")
        candidates = []
        for name, event_id in (("A", event.candidate_a_generation_id),
                               ("B", event.candidate_b_generation_id)):
            candidate = self.get_generation(event_id, owner_id)
            if candidate is None or candidate.session_id != session_id:
                reasons.append(f"candidate {name} is missing or outside this owner/session")
                continue
            candidates.append(candidate)
            if (candidate.context_snapshot_id != event.context_snapshot_id
                    or candidate.evidence_snapshot_id != event.evidence_snapshot_id):
                reasons.append(f"candidate {name} does not use the shared snapshots")
            messages = candidate.prompt_messages
            if (not messages or not any(m.get("content", "").strip() for m in messages)
                    or any(m.get("role") not in {"system", "user", "assistant", "tool"}
                           or "content" not in m for m in messages)):
                reasons.append(f"candidate {name} has no usable exact prompt")
        if len(candidates) == 2:
            # List order and exact string content matter. Do not trim/normalize.
            if candidates[0].prompt_messages != candidates[1].prompt_messages:
                reasons.append("candidate prompt messages differ")
            if candidates[0].prompt_version != candidates[1].prompt_version:
                reasons.append("candidate prompt versions differ")
        return reasons

    # -- reading ----------------------------------------------------------

    def _rows(self, sql: str, params: Iterable[Any]) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _payload(self, event_id: str, owner_id: str, kind: str) -> dict[str, Any] | None:
        rows = self._rows(
            "SELECT e.payload FROM events e JOIN sessions s USING (session_id)"
            " WHERE e.event_id = ? AND e.kind = ? AND s.owner_id = ?",
            (event_id, kind, owner_id))
        return json.loads(rows[0]["payload"]) if rows else None

    def get_context_snapshot(self, event_id: str, owner_id: str) -> ContextSnapshot | None:
        payload = self._payload(event_id, owner_id, "context_snapshot")
        return ContextSnapshot.model_validate(payload) if payload else None

    def evidence_for_context(self, context_id: str, owner_id: str) -> EvidenceSnapshot | None:
        # The planner assigns the context event ID as its paired bundle ID.
        rows = self._rows(
            "SELECT e.payload FROM events e JOIN sessions s USING (session_id)"
            " WHERE e.kind = 'evidence_snapshot' AND s.owner_id = ?"
            " AND json_extract(e.payload, '$.bundle.bundle_id') = ?"
            " ORDER BY e.rowid DESC LIMIT 1", (owner_id, context_id))
        return EvidenceSnapshot.model_validate_json(rows[0]["payload"]) if rows else None

    def latest_context(self, session_id: str, owner_id: str) -> ContextSnapshot | None:
        """The current planning state, derived rather than stored separately."""
        rows = self._rows(
            "SELECT e.payload FROM events e JOIN sessions s USING (session_id)"
            " WHERE e.session_id = ? AND s.owner_id = ? AND e.kind = 'context_snapshot'"
            " ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1",
            (session_id, owner_id))
        return ContextSnapshot.model_validate_json(rows[0]["payload"]) if rows else None

    def context_history(self, session_id: str, owner_id: str) -> list[ContextSnapshot]:
        rows = self._rows(
            "SELECT e.payload FROM events e JOIN sessions s USING (session_id)"
            " WHERE e.session_id = ? AND s.owner_id = ? AND e.kind = 'context_snapshot'"
            " ORDER BY e.created_at ASC, e.rowid ASC",
            (session_id, owner_id))
        return [ContextSnapshot.model_validate_json(r["payload"]) for r in rows]

    def get_generation(self, generation_id: str, owner_id: str) -> GenerationEvent | None:
        payload = self._payload(generation_id, owner_id, "generation")
        return GenerationEvent.model_validate(payload) if payload else None

    def get_preference(self, event_id: str, owner_id: str) -> PreferenceEvent | None:
        payload = self._payload(event_id, owner_id, "preference")
        return PreferenceEvent.model_validate(payload) if payload else None

    def generations(self, session_id: str, owner_id: str) -> list[GenerationEvent]:
        rows = self._rows(
            "SELECT e.payload FROM events e JOIN sessions s USING (session_id)"
            " WHERE e.session_id = ? AND s.owner_id = ? AND e.kind = 'generation'"
            " ORDER BY e.created_at ASC, e.rowid ASC",
            (session_id, owner_id))
        return [GenerationEvent.model_validate_json(r["payload"]) for r in rows]

    def revisions_for(self, generation_id: str, owner_id: str) -> list[RevisionEvent]:
        rows = self._rows(
            "SELECT e.payload FROM events e JOIN sessions s USING (session_id)"
            " WHERE e.generation_id = ? AND s.owner_id = ? AND e.kind = 'revision'"
            " ORDER BY e.created_at ASC, e.rowid ASC",
            (generation_id, owner_id))
        return [RevisionEvent.model_validate_json(r["payload"]) for r in rows]

    def replay(self, generation_id: str, owner_id: str
               ) -> tuple[ContextSnapshot, EvidenceSnapshot, GenerationEvent] | None:
        """Everything needed to reproduce one response exactly.

        This is the method the whole store exists for. An SFT row or a DPO pair
        is built from these three objects, not from today's corpus.
        """
        generation = self.get_generation(generation_id, owner_id)
        if generation is None:
            return None
        context = self._payload(generation.context_snapshot_id, owner_id,
                                "context_snapshot")
        evidence = self._payload(generation.evidence_snapshot_id, owner_id,
                                 "evidence_snapshot")
        if (context is None or evidence is None
                or context.get("session_id") != generation.session_id
                or evidence.get("session_id") != generation.session_id):
            return None
        return (ContextSnapshot.model_validate(context),
                EvidenceSnapshot.model_validate(evidence),
                generation)

    def count_events(self, session_id: str, owner_id: str,
                     kind: str | None = None) -> int:
        sql = ("SELECT COUNT(*) AS n FROM events e JOIN sessions s USING (session_id)"
               " WHERE e.session_id = ? AND s.owner_id = ?")
        params: list[Any] = [session_id, owner_id]
        if kind is not None:
            sql += " AND e.kind = ?"
            params.append(kind)
        return self._rows(sql, params)[0]["n"]

    # -- splits -----------------------------------------------------------

    def assign_split(self, family_id: str, split: str,
                     reason: str | None = None) -> None:
        """Assign a split to a whole family, before any variants are generated."""
        if split not in ("train", "dev", "test"):
            raise ValueError(f"unknown split: {split!r}")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO splits (family_id, split, assigned_at, reason)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(family_id) DO UPDATE SET"
                " split = excluded.split, assigned_at = excluded.assigned_at,"
                " reason = excluded.reason",
                (family_id, split, _now(), reason))

    def get_split(self, family_id: str) -> str | None:
        rows = self._rows("SELECT split FROM splits WHERE family_id = ?", (family_id,))
        return rows[0]["split"] if rows else None

    # -- export eligibility -----------------------------------------------

    def training_exclusions(self, generation_id: str, owner_id: str) -> list[str]:
        """Why this generation may not go into a training export.

        Both the inputs and the output are checked. Permission to use an asset
        in a classroom is not permission to put it in a dataset, so the two are
        tested separately.
        """
        return self._generation_exclusions(generation_id, owner_id, check_output=True)

    def _generation_exclusions(self, generation_id: str, owner_id: str,
                               *, check_output: bool) -> list[str]:
        reasons: list[str] = []
        replayed = self.replay(generation_id, owner_id)
        if replayed is None:
            return ["generation not found or its snapshots are missing"]
        context, evidence, generation = replayed

        rows = self._rows(
            "SELECT consent_model_training FROM sessions WHERE session_id = ?"
            " AND owner_id = ?", (generation.session_id, owner_id))
        if not rows or not rows[0]["consent_model_training"]:
            reasons.append("session has not consented to model training")

        for asset in evidence.bundle.community:
            if not asset.permissions.model_training:
                reasons.append(
                    f"community asset {asset.asset_id} is not cleared for "
                    f"model training")

        seeds = [(f"seed excerpt {s.source_id}", s)
                 for s in evidence.bundle.seed_excerpts]
        seed_slot = context.context.seed_lesson
        if seed_slot.value is not None:
            seeds.append((f"context seed {seed_slot.value.source_id}", seed_slot.value))
        for name, seed in seeds:
            if seed.training_use is not TrainingUseStatus.ALLOWED:
                reasons.append(f"{name} training use is {seed.training_use.value}")
            elif not (seed.training_permission_basis and seed.training_permission_basis.strip()):
                reasons.append(f"{name} has no recorded training permission basis")

        # Rejected DPO answers can intentionally contain a known quality error.
        # Their input permissions still apply; the chosen answer must pass.
        if check_output:
            if generation.validation is not None:
                errors = [f for f in generation.validation.findings
                          if f.severity is Severity.ERROR]
                if errors:
                    reasons.append(
                        f"validation reported {len(errors)} error(s): "
                        f"{errors[0].code}")
            else:
                reasons.append("generation was never validated")

        if self.get_split(self._family_of(generation.session_id)) is None:
            reasons.append("family has no split assigned")

        return reasons

    def preference_exclusions(self, event_id: str, owner_id: str) -> list[str]:
        """Structural/permission gate for DPO; not a complete curation policy.

        Recheck on every export, so old bad pairs and withdrawn permission fail
        closed. Callers must also select the intended split, audit label quality,
        and retain the provenance alongside trainer-formatted rows.
        """
        rows = self._rows(
            "SELECT e.session_id FROM events e JOIN sessions s USING (session_id)"
            " WHERE e.event_id = ? AND e.kind = 'preference' AND s.owner_id = ?",
            (event_id, owner_id))
        if not rows:
            return ["preference not found"]
        try:
            event = self.get_preference(event_id, owner_id)
            reasons = self._preference_input_errors(event, rows[0]["session_id"], owner_id)
        except (ValueError, TypeError):
            return ["preference or candidate payload is invalid"]
        if reasons:
            return reasons
        if event.label not in {PreferenceLabel.CHOSEN_A, PreferenceLabel.CHOSEN_B}:
            reasons.append("preference has no decisive winner")
        if event.label_origin is LabelOrigin.UNKNOWN:
            reasons.append("preference label origin is unknown")
        if event.label_origin is LabelOrigin.AI_FEEDBACK and not (
            event.judge_model and event.judge_model.strip()
            and event.judge_prompt_version and event.judge_prompt_version.strip()
        ):
            reasons.append("AI preference lacks judge model or prompt version")
        if event.label_origin is LabelOrigin.PROGRAMMATIC and not (
            event.rule_version and event.rule_version.strip()
        ):
            reasons.append("programmatic preference lacks rule version")
        candidate_ids = (event.candidate_a_generation_id, event.candidate_b_generation_id)
        chosen_id = (candidate_ids[0] if event.label is PreferenceLabel.CHOSEN_A
                     else candidate_ids[1])
        for candidate_id in candidate_ids:
            reasons.extend(f"{candidate_id}: {reason}" for reason in
                           self._generation_exclusions(candidate_id, owner_id,
                                                       check_output=candidate_id == chosen_id))
        candidates = [self.get_generation(i, owner_id) for i in candidate_ids]
        if any(not c.output_raw.strip() for c in candidates):
            reasons.append("candidate output is empty")
        elif candidates[0].output_raw == candidates[1].output_raw:
            reasons.append("candidate outputs are identical")
        return reasons

    def _family_of(self, session_id: str) -> str:
        rows = self._rows("SELECT family_id FROM sessions WHERE session_id = ?",
                          (session_id,))
        return rows[0]["family_id"] if rows else ""

    def eligible_generation_ids(self, owner_id: str,
                                split: str | None = None) -> list[str]:
        """Generation ids with no exclusion reason, optionally within one split."""
        sql = ("SELECT e.event_id FROM events e JOIN sessions s USING (session_id)"
               " WHERE e.kind = 'generation' AND s.owner_id = ?")
        params: list[Any] = [owner_id]
        if split is not None:
            sql += (" AND s.family_id IN (SELECT family_id FROM splits"
                    " WHERE split = ?)")
            params.append(split)
        sql += " ORDER BY e.created_at ASC, e.rowid ASC"
        ids = [r["event_id"] for r in self._rows(sql, params)]
        return [i for i in ids if not self.training_exclusions(i, owner_id)]

    def write_export_manifest(self, manifest: ExportManifest) -> str:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO export_manifests (export_id, dataset_version,"
                " created_at, payload) VALUES (?, ?, ?, ?)",
                (manifest.export_id, manifest.dataset_version,
                 manifest.created_at.isoformat(), manifest.model_dump_json()))
        return manifest.export_id

    def get_export_manifest(self, export_id: str) -> ExportManifest | None:
        rows = self._rows(
            "SELECT payload FROM export_manifests WHERE export_id = ?", (export_id,))
        return ExportManifest.model_validate_json(rows[0]["payload"]) if rows else None

    # -- withdrawal -------------------------------------------------------

    def redact_session(self, session_id: str, owner_id: str, reason: str) -> int:
        """Remove a session and its events, leaving a record that it happened.

        This is the only path that deletes an event, and it exists because a
        contributor may withdraw. It removes the material from future exports
        and from retrieval. It does not remove whatever a model trained on that
        material has already learnt, and nothing here should be described as if
        it does.
        """
        session = self.get_session(session_id, owner_id)
        if session is None:
            raise PermissionError(
                f"session {session_id!r} is not available to owner {owner_id!r}")
        with self._lock, self._conn:
            removed = self._conn.execute(
                "DELETE FROM events WHERE session_id = ?", (session_id,)).rowcount
            self._conn.execute("DELETE FROM sessions WHERE session_id = ?",
                               (session_id,))
            self._conn.execute(
                "INSERT INTO redactions (redaction_id, session_id, family_id,"
                " owner_id, redacted_at, events_removed, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_new_id("red"), session_id, session["family_id"], owner_id,
                 _now(), removed, reason))
        return removed

    def redactions(self, owner_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows(
            "SELECT * FROM redactions WHERE owner_id = ? ORDER BY redacted_at DESC",
            (owner_id,))]


def new_event_id(prefix: str = "evt") -> str:
    """Ids for the event models, which the store expects to be set already."""
    return _new_id(prefix)
