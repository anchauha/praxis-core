"""Integration tests with real corpus/store and a deterministic Ollama substitute."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi.testclient import TestClient
from backend import main
from backend.config import Settings, get_settings
from backend.ollama_client import OllamaClient, OllamaUnavailable
from backend.planning import build_prompt, prepare_context
from backend.retrieval import CorpusError, StandardsCatalog
from backend.schemas import PlanningContext

KEY = "IN|2023|Mathematics|Grade 6|6.RP.2"
COURSE = KEY.rsplit("|", 1)[0]


def context():
    def known(value):
        return {"value": value, "status": "known", "origin": "synthetic_scenario", "teacher_confirmed": False}
    return {"case_origin": "synthetic",
            "teacher_raw_intentions": known({"raw_text": "Plan a paper-based lesson on interpreting unit rates."}),
            "topic_subject": known({"topic": "Unit rates", "subject": "Mathematics"}),
            "discipline_target_standards": known({"subject": "Mathematics", "requested_standard_keys": [KEY]}),
            "classroom_community_profile": known({"grades": [6], "course": "Grade 6"}),
            "time_budget": known({"sessions": 1, "minutes_per_session": 45}),
            "output_deliverable_scope": known({"type": "single_lesson", "include": ["assessment"]}),
            "assessment_preference": known({"purpose": "formative"})}


def draft():
    return {"title": "Unit rates", "objectives": ["Interpret a unit rate"],
            "alignment": [{"standard_key": KEY, "evidence_ref": "S1", "learning_goal": "Interpret a unit rate",
                           "activity_evidence": "Compare liters per minute", "assessment_evidence": "Explain 12/3 = 4 L/min", "coverage": "partial"}],
            "materials": ["paper"], "sequence": [{"name": "Investigate", "minutes": 45, "description": "Compare volume and elapsed time."}],
            "assessment": {"purpose": "formative", "prompt": "Interpret 12 liters in 3 minutes.", "expected_evidence": "4 liters per minute."}}


class FakeOllama:
    output = None
    fail = False
    calls = 0
    last_payload = None
    done_reason = "stop"

    def __init__(self, settings): pass
    async def aclose(self): pass
    async def version(self): return "fixture"
    async def list_models(self):
        return [{"name": "fixture-local", "is_cloud": False, "digest": "fixture-digest"},
                {"name": "fixture-cloud", "is_cloud": True}]
    async def complete(self, payload):
        type(self).calls += 1
        type(self).last_payload = payload
        if self.fail: raise OllamaUnavailable("Simulated local failure")
        return {"message": {"content": self.output if self.output is not None else json.dumps(draft())},
                "done": True, "done_reason": self.done_reason, "prompt_eval_count": 1100, "eval_count": 500}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        s = get_settings()
        self.source = StandardsCatalog(s.standards_index, s.course_manifest)

    def test_counts_and_holds(self):
        self.assertEqual(len(self.source.courses()), 26)
        self.assertEqual(len(self.source.available), 711)
        self.assertEqual(sum(c["served"] for c in self.source.courses()), 24)
        for name in ("Quantitative Reasoning", "Environmental Science"):
            key = next(c["course_key"] for c in self.source.courses() if c["course"] == name)
            with self.subTest(course=name), self.assertRaises(CorpusError):
                self.source.search(key)

    def test_exact_code_and_keyword_search_stay_in_course(self):
        self.assertEqual(self.source.search(COURSE, "6.RP.2")[0].standard_key, KEY)
        results = self.source.search(COURSE, "rate")
        self.assertTrue(results)
        self.assertTrue(all(s.grade_or_course == "Grade 6" for s in results))

    def test_full_key_lookup_preserves_provenance(self):
        row = self.source.select([KEY])[0]
        self.assertEqual(row.page_number, 6)
        self.assertEqual(row.extraction_review_status, "reviewed")
        self.assertEqual(len(row.document_sha256), 64)

    def test_cannot_select_held_ambiguous_cross_course_or_repeated_keys(self):
        held = next(r["standard_key"] for r in self.source.index["standards"] if r["grade_or_course"] == "Quantitative Reasoning")
        other = next(k for k in self.source.available if not k.startswith(COURSE))
        for keys in ([], [KEY, KEY], [KEY, other], [held], ["fake"]):
            with self.subTest(keys=keys), self.assertRaises(CorpusError): self.source.select(keys)

    def test_changed_manifest_hash_fails_closed(self):
        self.source.manifest["documents"][0]["sha256"] = "changed"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps(self.source.manifest), encoding="utf-8")
            with self.assertRaises(CorpusError): StandardsCatalog(get_settings().standards_index, path)

    def test_prompt_retains_missingness_and_provenance_and_fits_small_case(self):
        ctx, evd = prepare_context(PlanningContext.model_validate(context()), "fixture", self.source)
        messages, byte_count = build_prompt(ctx.context, evd.bundle, num_ctx=8192, num_predict=2048)
        data = json.loads(messages[-1]["content"])
        self.assertEqual(data["planning_context"]["case_origin"], "synthetic")
        self.assertEqual(data["planning_context"]["field_state"]["time_budget"]["origin"], "synthetic_scenario")
        self.assertEqual(data["planning_context"]["unspecified"]["community_assets_cultural_wealth"], "unknown")
        self.assertLessEqual(byte_count + 2048 + 512, 8192)

    def test_oversized_prompt_is_refused_without_dropping_constraints(self):
        raw = context()
        raw["teacher_raw_intentions"]["value"]["raw_text"] = "Required constraint. " * 1000
        ctx, evd = prepare_context(PlanningContext.model_validate(raw), "fixture", self.source)
        with self.assertRaisesRegex(ValueError, "budget"):
            build_prompt(ctx.context, evd.bundle, num_ctx=8192, num_predict=2048)
        self.assertEqual(ctx.context.teacher_raw_intentions.value.raw_text, raw["teacher_raw_intentions"]["value"]["raw_text"])


class PlanningAPITests(unittest.TestCase):
    def setUp(self):
        FakeOllama.output, FakeOllama.fail, FakeOllama.calls, FakeOllama.done_reason = None, False, 0, "stop"
        self.temp = tempfile.TemporaryDirectory(prefix="praxis-api-")
        self.addCleanup(self.temp.cleanup)
        self.settings_patch = patch.object(main.settings, "store_path", Path(self.temp.name) / "test.sqlite")
        self.settings_patch.start(); self.addCleanup(self.settings_patch.stop)
        self.client_patch = patch.object(main, "OllamaClient", FakeOllama)
        self.client_patch.start(); self.addCleanup(self.client_patch.stop)
        self.client = TestClient(main.app)
        self.client.__enter__(); self.addCleanup(self.client.__exit__, None, None, None)
        response = self.client.post("/api/planning/sessions", json={"title": "Rates"})
        self.assertEqual(response.status_code, 201)
        self.session = response.json()["session_id"]
        self.base = f"/api/planning/sessions/{self.session}"

    def save(self, raw=None):
        response = self.client.post(self.base + "/contexts", json={"context": raw or context()})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def generate(self, saved=None, model="fixture-local"):
        saved = saved or self.save()
        return self.client.post(self.base + "/generations", json={"context_snapshot_id": saved["context_snapshot"]["event_id"], "model": model})

    def test_browser_cookie_is_opaque_and_not_exposed_in_session_rows(self):
        cookie = self.client.cookies.get("praxis_planning_owner")
        self.assertEqual(len(cookie), 43)
        rows = self.client.get("/api/planning/sessions").json()
        self.assertNotIn("owner_id", rows[0])

    def test_save_reload_and_generate_records_exact_inputs(self):
        saved = self.save()
        loaded = self.client.get(self.base).json()
        self.assertEqual(loaded["context_snapshot"], saved["context_snapshot"])
        self.assertEqual(loaded["evidence_snapshot"], saved["evidence_snapshot"])
        result = self.generate(saved)
        self.assertEqual(result.status_code, 201, result.text)
        event = result.json()
        self.assertEqual(event["prompt_messages"], FakeOllama.last_payload["messages"])
        self.assertEqual(event["model_options"]["request"], FakeOllama.last_payload)
        self.assertEqual(event["plan"]["source_verification_status"], "verified")
        self.assertEqual(event["plan"]["alignment_status"], "unverified")
        owner = self.client.cookies.get("praxis_planning_owner")
        self.assertTrue(main.app.state.store.training_exclusions(event["event_id"], owner))

    def test_generations_share_saved_snapshots_and_exact_prompt(self):
        saved = self.save()
        a, b = self.generate(saved).json(), self.generate(saved).json()
        self.assertEqual(a["context_snapshot_id"], b["context_snapshot_id"])
        self.assertEqual(a["evidence_snapshot_id"], b["evidence_snapshot_id"])
        self.assertEqual(a["prompt_messages"], b["prompt_messages"])

    def test_different_browser_cannot_read_or_use_session(self):
        saved = self.save()
        self.client.cookies.clear()
        self.assertEqual(self.client.get(self.base).status_code, 404)
        self.assertEqual(self.generate(saved).status_code, 404)
        self.assertEqual(self.client.get("/api/planning/sessions").json(), [])

    def test_context_from_another_session_cannot_be_generated(self):
        saved = self.save()
        other = self.client.post("/api/planning/sessions", json={"title": "Other"}).json()["session_id"]
        response = self.client.post(f"/api/planning/sessions/{other}/generations", json={
            "context_snapshot_id": saved["context_snapshot"]["event_id"], "model": "fixture-local"})
        self.assertEqual(response.status_code, 404)

    def test_cloud_and_uninstalled_models_rejected(self):
        saved = self.save()
        for model in ("fixture-cloud", "unknown"):
            with self.subTest(model=model): self.assertEqual(self.generate(saved, model).status_code, 422)
        self.assertEqual(FakeOllama.calls, 0)

    def test_invalid_json_and_truncated_output_are_saved_as_failures(self):
        saved = self.save()
        for output, reason in (("not JSON", "stop"), (json.dumps(draft()), "length")):
            FakeOllama.output, FakeOllama.done_reason = output, reason
            result = self.generate(saved).json()
            self.assertEqual(result["output_raw"], output)
            self.assertTrue(any(f["severity"] == "error" for f in result["validation"]["findings"]))

    def test_model_failure_preserves_saved_context_and_failed_run(self):
        saved = self.save(); FakeOllama.fail = True
        result = self.generate(saved).json()
        self.assertEqual(result["validation"]["findings"][0]["code"], "generation_failed")
        loaded = self.client.get(self.base).json()
        self.assertEqual(len(loaded["generations"]), 1)
        self.assertEqual(loaded["context_snapshot"], saved["context_snapshot"])

    def test_missing_alignment_is_a_failed_draft(self):
        incomplete = draft(); incomplete.pop("alignment")
        FakeOllama.output = json.dumps(incomplete)
        result = self.generate().json()
        self.assertTrue(any(f["code"] == "missing_alignment" and f["severity"] == "error"
                            for f in result["validation"]["findings"]))

    def test_generation_rechecks_current_serving_decisions(self):
        saved = self.save()
        s = get_settings(); manifest = json.loads(s.course_manifest.read_text(encoding="utf-8"))
        next(d for d in manifest["documents"] if d["course_key"] == COURSE)["served"] = False
        path = Path(self.temp.name) / "restricted.json"; path.write_text(json.dumps(manifest), encoding="utf-8")
        with patch.object(s, "course_manifest", path): self.assertEqual(self.generate(saved).status_code, 422)
        self.assertEqual(FakeOllama.calls, 0)

    def test_revision_is_appended_revalidated_and_does_not_replace_generation(self):
        generated = self.generate().json()
        edited = draft(); edited["sequence"][0]["minutes"] = 90
        response = self.client.post(self.base + f"/generations/{generated['event_id']}/revisions", json={"draft": edited})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue(any(f["code"] == "over_time_budget" for f in response.json()["edited_plan"]["validation"]["findings"]))
        loaded = self.client.get(self.base).json()
        self.assertEqual(loaded["generations"][0]["plan"]["sequence"][0]["minutes"], 45)
        self.assertEqual(len(loaded["revisions"][generated["event_id"]]), 1)

    def test_seed_training_permission_cannot_be_granted_through_planning_form(self):
        raw = context(); raw["seed_lesson"] = {"status": "known", "value": {"permitted_excerpts": ["Adapt this."],
            "training_use": "allowed", "training_permission_basis": "Caller asserted permission"}}
        saved = self.save(raw)
        self.assertEqual(saved["context_snapshot"]["context"]["seed_lesson"]["value"]["training_use"], "unknown")
        self.assertEqual(saved["evidence_snapshot"]["bundle"]["seed_excerpts"][0]["training_use"], "unknown")

    def test_source_review_claim_from_client_is_reset(self):
        raw = context(); raw["discipline_target_standards"]["value"]["alignment_status"] = "verified"
        result = self.save(raw)
        self.assertEqual(result["context_snapshot"]["context"]["discipline_target_standards"]["value"]["alignment_status"], "unverified")

    def test_new_home_and_general_chat_are_both_available(self):
        self.assertIn("planning-form", self.client.get("/").text)
        self.assertIn("composer", self.client.get("/chat").text)


class OllamaRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_retry_preserves_schema_and_records_removed_think_flag(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            if len(seen) == 1: return httpx.Response(400, json={"error": "think is not supported"})
            return httpx.Response(200, json={"done": True, "message": {"content": "{}"}})
        client = OllamaClient(Settings())
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost")
        payload = {"model": "fixture", "messages": [{"role": "user", "content": "Draft"}],
                   "format": {"type": "object"}, "think": False, "stream": False}
        try: await client.complete(payload)
        finally: await client.aclose()
        self.assertEqual(len(seen), 2)
        self.assertNotIn("think", payload)
        self.assertEqual(seen[1]["format"], {"type": "object"})


if __name__ == "__main__": unittest.main()
