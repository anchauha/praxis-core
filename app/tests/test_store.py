"""Tests for the event store.

Runs standalone with no test runner installed:

    .venv/Scripts/python.exe tests/test_store.py

and is picked up by pytest unchanged once that is added.
"""

import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.schemas import (  # noqa: E402
    AssetPermissions, ClassroomCommunityProfile, CommunityEvidence,
    ContextSnapshot, EvidenceBundle, EvidenceSnapshot, ExportManifest,
    FieldOrigin, FieldStatus, GenerationEvent, LessonPlan, LessonPlanDraft,
    PlanningContext, PreferenceEvent, PreferenceLabel, Provenanced,
    RevisionEvent, Severity, TeacherRawIntentions, ValidationFinding,
    ValidationReport,
)
from backend.store import Store, new_event_id  # noqa: E402

OWNER = "teacher-a"
OTHER = "teacher-b"


def fresh_store() -> Store:
    path = Path(tempfile.mkdtemp(prefix="catpc-test-")) / "events.sqlite"
    return Store(path)


def a_context(session_id: str, note: str) -> ContextSnapshot:
    return ContextSnapshot(
        event_id=new_event_id("ctx"),
        session_id=session_id,
        raw_request=note,
        context=PlanningContext(
            session_id=session_id,
            teacher_raw_intentions=Provenanced[TeacherRawIntentions](
                value=TeacherRawIntentions(raw_text=note),
                status=FieldStatus.KNOWN, origin=FieldOrigin.TEACHER),
            classroom_community_profile=Provenanced[ClassroomCommunityProfile](
                value=ClassroomCommunityProfile(grades=[6]),
                status=FieldStatus.KNOWN, origin=FieldOrigin.TEACHER),
        ),
    )


def an_evidence(session_id: str, *, training_ok: bool = True) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        event_id=new_event_id("evd"),
        session_id=session_id,
        standards_index_sha256="a" * 64,
        bundle=EvidenceBundle(
            bundle_id="b1",
            community=[CommunityEvidence(
                evidence_ref="C1", asset_id="garden-2026-04",
                contributor_description="Fill times for three containers.",
                permissions=AssetPermissions(classroom_generation=True,
                                             model_training=training_ok))],
        ),
    )


def a_generation(session_id: str, ctx_id: str, evd_id: str,
                 *, clean: bool = True) -> GenerationEvent:
    findings = [] if clean else [ValidationFinding(
        code="over_time_budget", severity=Severity.ERROR, message="too long")]
    return GenerationEvent(
        event_id=new_event_id("gen"),
        session_id=session_id,
        context_snapshot_id=ctx_id,
        evidence_snapshot_id=evd_id,
        prompt_messages=[{"role": "user", "content": "plan a lesson"}],
        prompt_version="p1",
        model="qwen3.5:9b",
        model_options={"num_ctx": 8192},
        output_raw='{"title": "Rates"}',
        plan=LessonPlan.promote(LessonPlanDraft(title="Rates",
                                                objectives=["Find a unit rate."])),
        validation=ValidationReport(findings=findings),
    )


def seeded(store: Store, *, consent: bool = True, training_ok: bool = True,
           clean: bool = True, split: str | None = "train") -> tuple[str, str]:
    session_id = store.create_session(OWNER, consent_model_training=consent)
    ctx = a_context(session_id, "first request")
    evd = an_evidence(session_id, training_ok=training_ok)
    store.append_context_snapshot(ctx, OWNER)
    store.append_evidence_snapshot(evd, OWNER)
    gen = a_generation(session_id, ctx.event_id, evd.event_id, clean=clean)
    store.append_generation(gen, OWNER)
    if split:
        store.assign_split(session_id, split, "test fixture")
    return session_id, gen.event_id


# --------------------------------------------------------------------------


def test_schema_initialises():
    store = fresh_store()
    assert store.schema_version == 1
    store.initialise()  # idempotent
    assert store.schema_version == 1


def test_session_defaults_to_its_own_family():
    store = fresh_store()
    session_id = store.create_session(OWNER, title="Rates")
    row = store.get_session(session_id, OWNER)
    assert row is not None
    assert row["family_id"] == session_id
    assert row["consent_classroom_use"] == 1
    assert row["consent_model_training"] == 0


def test_sessions_can_share_a_family():
    store = fresh_store()
    first = store.create_session(OWNER)
    second = store.create_session(OWNER, family_id=first)
    assert store.get_session(second, OWNER)["family_id"] == first


def test_another_owner_sees_nothing():
    store = fresh_store()
    session_id = store.create_session(OWNER)
    assert store.get_session(session_id, OTHER) is None
    assert store.list_sessions(OTHER) == []


def test_appending_to_another_owners_session_is_refused():
    store = fresh_store()
    session_id = store.create_session(OWNER)
    try:
        store.append_context_snapshot(a_context(session_id, "x"), OTHER)
    except PermissionError:
        return
    raise AssertionError("expected PermissionError")


def test_context_history_is_kept():
    store = fresh_store()
    session_id = store.create_session(OWNER)
    store.append_context_snapshot(a_context(session_id, "first"), OWNER)
    store.append_context_snapshot(a_context(session_id, "second"), OWNER)
    store.append_context_snapshot(a_context(session_id, "third"), OWNER)

    history = store.context_history(session_id, OWNER)
    assert [h.raw_request for h in history] == ["first", "second", "third"]
    assert store.latest_context(session_id, OWNER).raw_request == "third"
    assert store.count_events(session_id, OWNER, "context_snapshot") == 3


def test_events_cannot_be_updated():
    store = fresh_store()
    session_id, gen_id = seeded(store)
    try:
        with store._lock, store._conn:
            store._conn.execute(
                "UPDATE events SET payload = '{}' WHERE event_id = ?", (gen_id,))
    except sqlite3.IntegrityError as exc:
        assert "immutable" in str(exc)
        return
    raise AssertionError("expected the immutability trigger to fire")


def test_replay_returns_the_original_inputs():
    store = fresh_store()
    session_id, gen_id = seeded(store)

    # A later context snapshot must not change what the generation replays.
    store.append_context_snapshot(a_context(session_id, "much later request"), OWNER)

    replayed = store.replay(gen_id, OWNER)
    assert replayed is not None
    context, evidence, generation = replayed
    assert context.raw_request == "first request"
    assert evidence.standards_index_sha256 == "a" * 64
    assert generation.prompt_messages == [{"role": "user", "content": "plan a lesson"}]
    assert generation.model == "qwen3.5:9b"
    assert generation.plan.title == "Rates"


def test_replay_is_scoped_to_the_owner():
    store = fresh_store()
    _session_id, gen_id = seeded(store)
    assert store.replay(gen_id, OTHER) is None
    assert store.get_generation(gen_id, OTHER) is None


def test_revision_links_to_its_generation():
    store = fresh_store()
    session_id, gen_id = seeded(store)
    revision = RevisionEvent(
        event_id=new_event_id("rev"),
        generation_id=gen_id,
        edited_plan=LessonPlan.promote(LessonPlanDraft(title="Rates, shortened")),
        introduced_new_requirements=True,
        review_status="reviewed",
    )
    store.append_revision(revision, session_id, OWNER)
    found = store.revisions_for(gen_id, OWNER)
    assert len(found) == 1
    assert found[0].edited_plan.title == "Rates, shortened"
    assert found[0].introduced_new_requirements is True


def test_preference_records_one_shared_input():
    store = fresh_store()
    session_id, gen_a = seeded(store)
    ctx = store.latest_context(session_id, OWNER)
    evd_id = store.get_generation(gen_a, OWNER).evidence_snapshot_id
    gen_b = a_generation(session_id, ctx.event_id, evd_id)
    store.append_generation(gen_b, OWNER)

    pref = PreferenceEvent(
        event_id=new_event_id("prf"),
        context_snapshot_id=ctx.event_id,
        evidence_snapshot_id=evd_id,
        candidate_a_generation_id=gen_a,
        candidate_b_generation_id=gen_b.event_id,
        label=PreferenceLabel.TIE,
        rationale="Both acceptable.",
    )
    store.append_preference(pref, session_id, OWNER)
    assert store.count_events(session_id, OWNER, "preference") == 1


def test_splits_are_assigned_per_family():
    store = fresh_store()
    first = store.create_session(OWNER)
    second = store.create_session(OWNER, family_id=first)
    store.assign_split(first, "test", "held out")
    assert store.get_split(first) == "test"
    assert store.get_split(store.get_session(second, OWNER)["family_id"]) == "test"
    store.assign_split(first, "train", "changed my mind")
    assert store.get_split(first) == "train"
    try:
        store.assign_split(first, "validation")
    except ValueError:
        return
    raise AssertionError("expected ValueError for an unknown split")


def test_clean_generation_has_no_exclusions():
    store = fresh_store()
    _session_id, gen_id = seeded(store)
    assert store.training_exclusions(gen_id, OWNER) == []
    assert store.eligible_generation_ids(OWNER) == [gen_id]
    assert store.eligible_generation_ids(OWNER, split="train") == [gen_id]
    assert store.eligible_generation_ids(OWNER, split="test") == []


def test_missing_session_consent_excludes():
    store = fresh_store()
    _session_id, gen_id = seeded(store, consent=False)
    reasons = store.training_exclusions(gen_id, OWNER)
    assert any("consented to model training" in r for r in reasons)
    assert store.eligible_generation_ids(OWNER) == []


def test_classroom_permission_is_not_training_permission():
    store = fresh_store()
    _session_id, gen_id = seeded(store, training_ok=False)
    reasons = store.training_exclusions(gen_id, OWNER)
    assert any("not cleared for model training" in r for r in reasons)


def test_validation_errors_exclude():
    store = fresh_store()
    _session_id, gen_id = seeded(store, clean=False)
    reasons = store.training_exclusions(gen_id, OWNER)
    assert any("validation reported" in r for r in reasons)


def test_unassigned_split_excludes():
    store = fresh_store()
    _session_id, gen_id = seeded(store, split=None)
    reasons = store.training_exclusions(gen_id, OWNER)
    assert any("no split assigned" in r for r in reasons)


def test_withdrawing_consent_stops_future_exports():
    store = fresh_store()
    session_id, gen_id = seeded(store)
    assert store.training_exclusions(gen_id, OWNER) == []
    assert store.set_consent(session_id, OWNER, consent_model_training=False)
    assert store.training_exclusions(gen_id, OWNER) != []
    try:
        store.set_consent(session_id, OWNER, consent_to_everything=True)
    except ValueError:
        return
    raise AssertionError("expected ValueError for an unknown consent flag")


def test_redaction_removes_events_and_leaves_a_record():
    store = fresh_store()
    session_id, gen_id = seeded(store)
    assert store.count_events(session_id, OWNER) == 3

    removed = store.redact_session(session_id, OWNER, "contributor withdrew")
    assert removed == 3
    assert store.get_session(session_id, OWNER) is None
    assert store.replay(gen_id, OWNER) is None

    records = store.redactions(OWNER)
    assert len(records) == 1
    assert records[0]["events_removed"] == 3
    assert records[0]["reason"] == "contributor withdrew"


def test_redaction_is_scoped_to_the_owner():
    store = fresh_store()
    session_id, _gen_id = seeded(store)
    try:
        store.redact_session(session_id, OTHER, "not mine")
    except PermissionError:
        assert store.count_events(session_id, OWNER) == 3
        return
    raise AssertionError("expected PermissionError")


def test_export_manifest_round_trip():
    store = fresh_store()
    _session_id, gen_id = seeded(store)
    manifest = ExportManifest(
        export_id="exp-1", dataset_version="0.1.0",
        included_event_ids=[gen_id],
        split_assignments={gen_id: "train"},
        preprocessing_versions={"serialiser": "0.1.0"},
    )
    store.write_export_manifest(manifest)
    back = store.get_export_manifest("exp-1")
    assert back == manifest


def test_store_survives_reopening():
    store = fresh_store()
    session_id, gen_id = seeded(store)
    path = store.path
    store.close()

    reopened = Store(path)
    assert reopened.latest_context(session_id, OWNER).raw_request == "first request"
    assert reopened.replay(gen_id, OWNER) is not None
    assert reopened.get_split(session_id) == "train"


# --------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  pass  {name}")
        except Exception:
            failures += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
