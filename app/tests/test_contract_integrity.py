"""Regression tests for provenance, source binding, preferences and seed rights.

Run from app: python -B -m unittest discover -s tests -p test_contract_integrity.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError
from backend.schemas import (
    AlignmentStatus, CommunityEvidence, ContextSnapshot, EvidenceBundle,
    FieldOrigin, FieldStatus, LabelOrigin, LessonPlan, LessonPlanDraft, Message,
    PlanningContext, PreferenceEvent, Provenanced, ResearchEvidence, SeedExcerpt,
    SeedLesson, SourceVerificationStatus, TopicSubject, ValidationReport,
)
from backend.store import Store, new_event_id
from backend.validation import validate_plan
from backend.schemas import StandardEvidence
from test_store import OWNER, OTHER, a_context, an_evidence, a_generation


def standard(ref='S1', key='IN|2023|Mathematics|Grade 6|6.RP.2', **changes):
    values = dict(evidence_ref=ref, standard_key=key, printed_code='6.RP.2',
                  jurisdiction='IN', standards_version='2023', subject='Mathematics',
                  grade_or_course='Grade 6', text='Interpret a unit rate.',
                  source_url='https://example.invalid/fixture.pdf', source_file='fixture.pdf',
                  page_number=6, document_sha256='a' * 64, extraction_review_status='reviewed')
    return StandardEvidence(**(values | changes))


def plan(ref='S1', key='IN|2023|Mathematics|Grade 6|6.RP.2'):
    # Deliberately unrelated instructional text: source checks cannot certify it.
    return LessonPlan(title='Classifying triangles', objectives=['Classify triangles'],
                      alignment=[dict(standard_key=key, evidence_ref=ref,
                                      learning_goal='Classify triangles',
                                      activity_evidence='Sort triangle cards',
                                      assessment_evidence='Name each triangle', coverage='full')])


class ContractTests(unittest.TestCase):
    def test_synthetic_and_suggested_values_retain_provenance_in_both_views(self):
        for origin in (FieldOrigin.SYNTHETIC_SCENARIO, FieldOrigin.SYSTEM_SUGGESTION):
            with self.subTest(origin=origin):
                context = PlanningContext(case_origin='synthetic', topic_subject=Provenanced[TopicSubject](
                    value=TopicSubject(topic='Unit rates'), status='known', origin=origin,
                    source_refs=['fixture:1'], note='A fictional proposal'))
                restored = PlanningContext.model_validate_json(context.model_dump_json())
                for view in (restored.to_flat_export(), restored.to_prompt_view()):
                    self.assertEqual(view['case_origin'], 'synthetic')
                    state = view['field_state']['topic_subject']
                    self.assertEqual(state['origin'], origin.value)
                    self.assertFalse(state['teacher_confirmed'])
                    self.assertEqual(state['source_refs'], ['fixture:1'])
                    self.assertEqual(state['note'], 'A fictional proposal')

    def test_withheld_value_and_associated_notes_do_not_enter_prompt(self):
        context = PlanningContext(topic_subject=Provenanced[TopicSubject](
            value=TopicSubject(topic='private'), status='withheld', note='private note',
            source_refs=['private ref']))
        view = context.to_prompt_view()
        self.assertNotIn('private', json.dumps(view))
        self.assertEqual(view['unspecified']['topic_subject'], 'withheld')

    def test_synthetic_values_cannot_claim_teacher_confirmation(self):
        with self.assertRaises(ValidationError):
            Provenanced[TopicSubject](origin='synthetic_scenario', teacher_confirmed=True)

    def test_community_provenance_round_trips(self):
        asset = CommunityEvidence(evidence_ref='C1', asset_id='fiction',
                                  contributor_description='Fictional data',
                                  asset_origin='synthetic_scenario',
                                  community_validation_status='not_validated')
        result = CommunityEvidence.model_validate_json(asset.model_dump_json())
        self.assertEqual(result.asset_origin, 'synthetic_scenario')
        self.assertEqual(result.community_validation_status, 'not_validated')

    def test_synthetic_asset_cannot_claim_community_review(self):
        with self.assertRaises(ValidationError):
            CommunityEvidence(evidence_ref='C1', asset_id='fiction', contributor_description='Fiction',
                              asset_origin='synthetic_scenario', community_validation_status='community_reviewed')

    def test_misspelled_provenance_is_rejected_instead_of_dropped(self):
        with self.assertRaises(ValidationError):
            CommunityEvidence(evidence_ref='C1', asset_id='fiction', contributor_description='Fiction',
                              asset_orign='synthetic_scenario')

    def test_legacy_origins_are_not_inferred(self):
        context = PlanningContext.model_validate({'schema_version': '0.1.0'})
        asset = CommunityEvidence(evidence_ref='C1', asset_id='old', contributor_description='Unspecified')
        self.assertEqual(context.case_origin, 'unknown')
        self.assertEqual(asset.asset_origin, 'unknown')
        self.assertEqual(asset.community_validation_status, 'not_validated')

    def test_source_review_does_not_certify_unrelated_instruction(self):
        report = validate_plan(plan(), PlanningContext(), EvidenceBundle(standards=[standard()]))
        self.assertEqual(report.source_verification_status, SourceVerificationStatus.VERIFIED)
        self.assertEqual(report.alignment_status, AlignmentStatus.UNVERIFIED)

    def test_research_ref_cannot_substitute_for_standard_ref(self):
        evidence = EvidenceBundle(standards=[standard()], research=[ResearchEvidence(
            evidence_ref='R1', framework='UDL', claim='A framework claim', citation='Fixture')])
        report = validate_plan(plan(ref='R1'), PlanningContext(), evidence)
        self.assertIn('standard_evidence_mismatch', [f.code for f in report.errors])
        self.assertEqual(report.source_verification_status, 'unverified')

    def test_another_standard_ref_cannot_substitute(self):
        evidence = EvidenceBundle(standards=[standard(), standard('S2', 'another-key')])
        report = validate_plan(plan(ref='S2'), PlanningContext(), evidence)
        self.assertIn('standard_evidence_mismatch', [f.code for f in report.errors])

    def test_duplicate_refs_across_groups_fail_closed(self):
        evidence = EvidenceBundle(standards=[standard()], research=[ResearchEvidence(
            evidence_ref='S1', framework='UDL', claim='A framework claim', citation='Fixture')])
        report = validate_plan(plan(), PlanningContext(), evidence)
        self.assertIn('duplicate_evidence_ref', [f.code for f in report.errors])
        self.assertEqual(report.source_verification_status, 'unverified')

    def test_duplicate_keys_do_not_overwrite_or_verify(self):
        evidence = EvidenceBundle(standards=[standard(), standard('S2', text='A different demand')])
        report = validate_plan(plan(), PlanningContext(), evidence)
        self.assertIn('ambiguous_standard_key', [f.code for f in report.findings])
        self.assertEqual(report.source_verification_status, 'provisional')

    def test_unreviewed_or_ambiguous_source_stays_provisional(self):
        for changes in ({'extraction_review_status': 'unreviewed'}, {'standard_key_ambiguous': True}):
            with self.subTest(changes=changes):
                report = validate_plan(plan(), PlanningContext(), EvidenceBundle(standards=[standard(**changes)]))
                self.assertEqual(report.source_verification_status, 'provisional')

    def test_missing_ref_fails_without_key_error(self):
        report = validate_plan(plan(ref='missing'), PlanningContext(), EvidenceBundle())
        self.assertFalse(report.passed)
        self.assertEqual(report.source_verification_status, 'unverified')

    def test_draft_cannot_self_assign_either_verification(self):
        for field in ('alignment_status', 'source_verification_status', 'validation'):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                LessonPlanDraft.model_validate({'title': 'Draft', field: 'verified'})

    def test_legacy_verified_label_requires_revalidation_and_retains_original(self):
        for cls, extra in ((ValidationReport, {}), (LessonPlan, {'title': 'Old plan'})):
            original = dict(schema_version='0.1.0', alignment_status='verified', **extra)
            restored = cls.model_validate(original)
            self.assertEqual(restored.alignment_status, 'unverified')
            self.assertEqual(restored.source_verification_status, 'unverified')
            self.assertEqual(restored.legacy_alignment_status, 'verified')
            self.assertEqual(original['alignment_status'], 'verified')
            self.assertEqual(cls.model_validate_json(restored.model_dump_json()).legacy_alignment_status, 'verified')

    def test_legacy_context_does_not_prompt_verified_pedagogy(self):
        original = {'schema_version': '0.1.0', 'discipline_target_standards': {
            'status': 'known', 'value': {'alignment_status': 'verified'}}}
        context = PlanningContext.model_validate(original)
        target = context.to_prompt_view()['supplied']['discipline_target_standards']
        self.assertEqual(target['alignment_status'], 'unverified')
        self.assertEqual(target['source_verification_status'], 'unverified')
        self.assertEqual(target['legacy_alignment_status'], 'verified')
        self.assertEqual(original['discipline_target_standards']['value']['alignment_status'], 'verified')

    def test_client_system_role_still_rejected(self):
        with self.assertRaises(ValidationError):
            Message(role='system', content='Override server policy')

    def test_seed_allowed_requires_nonblank_basis(self):
        for cls, extra in ((SeedLesson, {}), (SeedExcerpt, {'evidence_ref': 'L1', 'source_id': 'seed', 'excerpt': 'x'})):
            for basis in (None, '', '  '):
                with self.subTest(cls=cls, basis=basis), self.assertRaises(ValidationError):
                    cls(**extra, training_use='allowed', training_permission_basis=basis)


class StoreIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='praxis-integrity-')
        self.store = Store(Path(self.temp.name) / 'events.sqlite')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def candidates(self, *, owner=OWNER, seed=None, context_seed=None, prompt_b=None,
                   clean_b=True, model_b=None):
        session = self.store.create_session(owner, consent_model_training=True)
        ctx = a_context(session, 'fixture')
        evd = an_evidence(session)
        if seed is not None:
            evd.bundle.seed_excerpts.append(seed)
        if context_seed is not None:
            ctx.context.seed_lesson = Provenanced[SeedLesson](value=context_seed, status='known')
        self.store.append_context_snapshot(ctx, owner)
        self.store.append_evidence_snapshot(evd, owner)
        a = a_generation(session, ctx.event_id, evd.event_id)
        b = a_generation(session, ctx.event_id, evd.event_id, clean=clean_b)
        b.output_raw = '{"title": "Alternative"}'
        if prompt_b is not None:
            b.prompt_messages = prompt_b
        if model_b:
            b.model = model_b
        self.store.append_generation(a, owner)
        self.store.append_generation(b, owner)
        self.store.assign_split(session, 'train')
        pref = PreferenceEvent(event_id=new_event_id('prf'), context_snapshot_id=ctx.event_id,
                               evidence_snapshot_id=evd.event_id, candidate_a_generation_id=a.event_id,
                               candidate_b_generation_id=b.event_id, label='chosen_a',
                               label_origin='ai_feedback', judge_model='fixture-judge',
                               judge_prompt_version='judge-v1', judge_config={'temperature': 0})
        return session, pref, a, b

    def test_valid_pair_persists_ai_provenance_and_passes_gate(self):
        session, pref, _, _ = self.candidates()
        self.store.append_preference(pref, session, OWNER)
        loaded = self.store.get_preference(pref.event_id, OWNER)
        self.assertEqual(loaded.label_origin, LabelOrigin.AI_FEEDBACK)
        self.assertEqual(loaded.judge_model, 'fixture-judge')
        self.assertEqual(loaded.judge_prompt_version, 'judge-v1')
        self.assertEqual(loaded.judge_config, {'temperature': 0})
        self.assertEqual(self.store.preference_exclusions(pref.event_id, OWNER), [])
        self.assertIsNone(self.store.get_preference(pref.event_id, OTHER))

    def test_prompt_content_difference_is_rejected(self):
        session, pref, _, _ = self.candidates(prompt_b=[{'role': 'user', 'content': 'plan a lesson '}])
        with self.assertRaisesRegex(ValueError, 'messages differ'):
            self.store.append_preference(pref, session, OWNER)
        self.assertEqual(self.store.count_events(session, OWNER, 'preference'), 0)

    def test_empty_prompt_is_rejected(self):
        session, pref, _, _ = self.candidates(prompt_b=[])
        with self.assertRaisesRegex(ValueError, 'usable exact prompt'):
            self.store.append_preference(pref, session, OWNER)

    def test_empty_message_content_is_rejected(self):
        session, pref, _, _ = self.candidates(prompt_b=[{'role': 'user', 'content': '  '}])
        with self.assertRaisesRegex(ValueError, 'usable exact prompt'):
            self.store.append_preference(pref, session, OWNER)

    def test_missing_and_wrong_kind_candidates_are_rejected(self):
        session, pref, _, _ = self.candidates()
        for event_id in ('missing', pref.context_snapshot_id):
            with self.subTest(event_id=event_id), self.assertRaisesRegex(ValueError, 'candidate B is missing'):
                self.store.append_preference(pref.model_copy(update={'candidate_b_generation_id': event_id}), session, OWNER)

    def test_same_generation_cannot_appear_twice(self):
        session, pref, a, _ = self.candidates()
        pref.candidate_b_generation_id = a.event_id
        with self.assertRaisesRegex(ValueError, 'distinct generations'):
            self.store.append_preference(pref, session, OWNER)

    def test_candidates_from_other_session_or_owner_are_rejected(self):
        session, pref, _, _ = self.candidates()
        for owner in (OWNER, OTHER):
            _, _, foreign, _ = self.candidates(owner=owner)
            with self.subTest(owner=owner), self.assertRaisesRegex(ValueError, 'outside this owner/session'):
                self.store.append_preference(pref.model_copy(update={'candidate_b_generation_id': foreign.event_id}), session, OWNER)

    def test_another_owner_cannot_append_pair(self):
        session, pref, _, _ = self.candidates()
        with self.assertRaises(PermissionError):
            self.store.append_preference(pref, session, OTHER)

    def test_changed_context_is_a_new_input(self):
        session, pref, _, _ = self.candidates()
        ctx = a_context(session, 'new constraint')
        self.store.append_context_snapshot(ctx, OWNER)
        pref.context_snapshot_id = ctx.event_id
        with self.assertRaisesRegex(ValueError, 'shared snapshots'):
            self.store.append_preference(pref, session, OWNER)

    def test_changed_evidence_is_a_new_input(self):
        session, pref, _, _ = self.candidates()
        evd = an_evidence(session)
        self.store.append_evidence_snapshot(evd, OWNER)
        pref.evidence_snapshot_id = evd.event_id
        with self.assertRaisesRegex(ValueError, 'shared snapshots'):
            self.store.append_preference(pref, session, OWNER)

    def test_missing_snapshot_is_rejected(self):
        session, pref, _, _ = self.candidates()
        pref.evidence_snapshot_id = 'missing'
        with self.assertRaisesRegex(ValueError, 'evidence_snapshot is missing'):
            self.store.append_preference(pref, session, OWNER)

    def test_changed_prompt_version_is_rejected(self):
        session, pref, _, b = self.candidates()
        changed = b.model_copy(update={'event_id': new_event_id('gen'), 'prompt_version': 'p2'})
        self.store.append_generation(changed, OWNER)
        pref.candidate_b_generation_id = changed.event_id
        with self.assertRaisesRegex(ValueError, 'versions differ'):
            self.store.append_preference(pref, session, OWNER)

    def test_different_generator_models_are_allowed_with_same_inputs(self):
        session, pref, _, _ = self.candidates(model_b='another-generator')
        self.store.append_preference(pref, session, OWNER)
        self.assertEqual(self.store.preference_exclusions(pref.event_id, OWNER), [])

    def test_old_invalid_pair_is_excluded_on_export_recheck(self):
        session, pref, _, _ = self.candidates(prompt_b=[{'role': 'user', 'content': 'different'}])
        # Simulate a row admitted by v0.1, bypassing the corrected public method.
        self.store._append('preference', session, OWNER, pref)
        self.assertIn('candidate prompt messages differ', self.store.preference_exclusions(pref.event_id, OWNER))

    def test_legacy_and_incomplete_ai_labels_are_excluded(self):
        session, pref, _, _ = self.candidates()
        for changes, expected in (({'label_origin': 'unknown'}, 'origin is unknown'),
                                  ({'judge_model': None}, 'lacks judge'),
                                  ({'judge_prompt_version': None}, 'lacks judge'),
                                  ({'label_origin': 'programmatic', 'rule_version': None}, 'lacks rule')):
            with self.subTest(changes=changes):
                data = pref.model_dump() | changes | {'event_id': new_event_id('prf')}
                event = PreferenceEvent.model_validate(data)
                self.store.append_preference(event, session, OWNER)
                self.assertTrue(any(expected in r for r in self.store.preference_exclusions(event.event_id, OWNER)))

    def test_ties_are_stored_but_not_exported_as_preferences(self):
        session, pref, _, _ = self.candidates()
        event = PreferenceEvent.model_validate(pref.model_dump() | {'label': 'tie'})
        self.store.append_preference(event, session, OWNER)
        self.assertIn('preference has no decisive winner', self.store.preference_exclusions(event.event_id, OWNER))

    def test_rejected_quality_error_is_useful_but_chosen_error_is_excluded(self):
        session, pref, _, _ = self.candidates(clean_b=False)
        self.store.append_preference(pref, session, OWNER)
        self.assertEqual(self.store.preference_exclusions(pref.event_id, OWNER), [])
        bad_winner = PreferenceEvent.model_validate(pref.model_dump() | {'event_id': new_event_id('prf'), 'label': 'chosen_b'})
        self.store.append_preference(bad_winner, session, OWNER)
        self.assertTrue(any('validation reported' in r for r in self.store.preference_exclusions(bad_winner.event_id, OWNER)))

    def test_withdrawal_stops_preference_exports(self):
        session, pref, _, _ = self.candidates()
        self.store.append_preference(pref, session, OWNER)
        self.store.set_consent(session, OWNER, consent_model_training=False)
        self.assertTrue(any('not consented' in r for r in self.store.preference_exclusions(pref.event_id, OWNER)))

    def test_nonempty_rights_prose_never_grants_training_permission(self):
        for rights in ('unknown', 'research only', 'no model training', 'CC BY 4.0'):
            with self.subTest(rights=rights):
                seed = SeedExcerpt(evidence_ref='L1', source_id='seed', excerpt='Lesson', rights=rights)
                session, pref, a, _ = self.candidates(seed=seed)
                self.store.append_preference(pref, session, OWNER)
                self.assertTrue(any('training use is unknown' in r for r in self.store.training_exclusions(a.event_id, OWNER)))
                self.assertTrue(any('training use is unknown' in r for r in self.store.preference_exclusions(pref.event_id, OWNER)))

    def test_denied_seed_is_excluded(self):
        seed = SeedExcerpt(evidence_ref='L1', source_id='seed', excerpt='Lesson', rights='Some license', training_use='denied')
        _, _, a, _ = self.candidates(seed=seed)
        self.assertTrue(any('training use is denied' in r for r in self.store.training_exclusions(a.event_id, OWNER)))

    def test_explicitly_allowed_seed_with_basis_passes(self):
        seed = SeedExcerpt(evidence_ref='L1', source_id='seed', excerpt='My lesson', training_use='allowed',
                           training_permission_basis='Developer-authored fixture; cleared for this test')
        _, _, a, _ = self.candidates(seed=seed)
        self.assertEqual(self.store.training_exclusions(a.event_id, OWNER), [])

    def test_context_seed_permission_is_checked_without_evidence_excerpt(self):
        seed = SeedLesson(source_id='seed', permitted_excerpts=['Lesson'], rights='Not training permission')
        _, _, a, _ = self.candidates(context_seed=seed)
        self.assertTrue(any('context seed seed training use is unknown' in r for r in self.store.training_exclusions(a.event_id, OWNER)))

    def test_cross_session_replay_fails_closed(self):
        session, pref, a, _ = self.candidates()
        _, foreign_pref, _, _ = self.candidates()
        bad = a.model_copy(update={'event_id': new_event_id('gen'), 'context_snapshot_id': foreign_pref.context_snapshot_id})
        self.store.append_generation(bad, OWNER)
        self.assertIsNone(self.store.replay(bad.event_id, OWNER))
        self.assertTrue(self.store.training_exclusions(bad.event_id, OWNER))


if __name__ == '__main__':
    unittest.main()
