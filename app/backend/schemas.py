"""Request, response and planning contracts.

Three contracts carry the planning work:

- ``PlanningContext``  the eleven teacher-supplied fields, each with its status
  and where the value came from.
- ``EvidenceBundle``   the exact records shown to the model, with provenance.
- ``LessonPlan``       the response, linking every claim back to an evidence id.

One rule shapes all three. Facts and permissions come from the application and
the standards manifest; proposals come from the model. The model references
evidence by id and never asserts that a citation is correct, that an alignment
is verified, or that an asset may be used. Those are set by code, which is why
``LessonPlanDraft`` exists separately from ``LessonPlan``.

Renaming a field here changes stored rows and training exports, not only code.
Bump ``SCHEMA_VERSION`` when you do.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "0.1.0"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------


class Message(BaseModel):
    # The client never needs to send a system message: the server prepends its
    # own. Accepting the role here would let a caller inject a second one into
    # the list forwarded to Ollama.
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    model: str | None = None
    think: bool = False
    temperature: float | None = None


class ModelInfo(BaseModel):
    name: str
    parameter_size: str | None = None
    family: str | None = None
    size_bytes: int | None = None
    is_cloud: bool = False


class Health(BaseModel):
    ollama_reachable: bool
    host: str
    version: str | None = None
    default_model: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class FieldStatus(str, Enum):
    """Why a field has no value, which is not the same as having none."""

    KNOWN = "known"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"
    WITHHELD = "withheld"


class FieldOrigin(str, Enum):
    TEACHER = "teacher"
    APPROVED_PROFILE = "approved_profile"
    RETRIEVED_SOURCE = "retrieved_source"
    SYSTEM_SUGGESTION = "system_suggestion"


class AlignmentStatus(str, Enum):
    """Set by ``validate_plan``. The model may not put a value here."""

    UNVERIFIED = "unverified"
    PROVISIONAL = "provisional"
    VERIFIED = "verified"


class Coverage(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    NOT_ADDRESSED = "not_addressed"


class DeliverableScope(str, Enum):
    ACTIVITY = "activity"
    SINGLE_LESSON = "single_lesson"
    UNIT_OUTLINE = "unit_outline"
    ASSESSMENT = "assessment"
    ADAPTATION_CRITIQUE = "adaptation_critique"


class AssessmentPurpose(str, Enum):
    FORMATIVE = "formative"
    SUMMATIVE = "summative"
    BOTH = "both"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class RubricDimension(str, Enum):
    """The eight dimensions from the evaluation rubric."""

    DISCIPLINARY_ACCURACY_AND_DEMAND = "disciplinary_accuracy_and_demand"
    STANDARDS_ALIGNMENT = "standards_alignment"
    FEASIBILITY = "feasibility"
    UDL_AND_ACCESS = "udl_and_access"
    COMMUNITY_AUTHENTICITY_AND_AGENCY = "community_authenticity_and_agency"
    CULTURAL_AND_LINGUISTIC_RESPECT = "cultural_and_linguistic_respect"
    ASSESSMENT_QUALITY = "assessment_quality"
    GROUNDING_AND_TEACHER_CONTROL = "grounding_and_teacher_control"


class CriticalFlag(str, Enum):
    """Rated separately, because a good average must not hide any of these."""

    FABRICATED_STANDARD = "fabricated_standard"
    STEM_ERROR = "stem_error"
    HARMFUL_STEREOTYPE = "harmful_stereotype"
    PROTOCOL_VIOLATION = "protocol_violation"
    UNSAFE_ACTIVITY = "unsafe_activity"
    EXCLUSION_DESPITE_KNOWN_NEED = "exclusion_despite_known_need"


class PreferenceLabel(str, Enum):
    CHOSEN_A = "chosen_a"
    CHOSEN_B = "chosen_b"
    TIE = "tie"
    BOTH_UNACCEPTABLE = "both_unacceptable"
    UNRESOLVED_DISAGREEMENT = "unresolved_disagreement"


# ---------------------------------------------------------------------------
# Provenance wrapper
# ---------------------------------------------------------------------------


class Provenanced[T](BaseModel):
    """One planning field, together with how much is actually known about it.

    A value of ``None`` on its own cannot say whether the teacher has not
    answered yet, the field does not apply to this request, or the teacher
    declined to share it. ``status`` carries that difference, and ``origin``
    records who supplied the value so a system suggestion is never mistaken for
    something the teacher confirmed.
    """

    value: T | None = None
    status: FieldStatus = FieldStatus.UNKNOWN
    origin: FieldOrigin | None = None
    teacher_confirmed: bool = False
    source_refs: list[str] = Field(default_factory=list)
    note: str | None = None

    @property
    def is_known(self) -> bool:
        return self.status is FieldStatus.KNOWN and self.value is not None


# ---------------------------------------------------------------------------
# The eleven fields
# ---------------------------------------------------------------------------


class ClassroomCommunityProfile(BaseModel):
    """Field 1. Aggregate instructional context, never student records."""

    grades: list[int] = Field(default_factory=list)
    course: str | None = None
    prior_knowledge: str | None = None
    access_needs: list[str] = Field(default_factory=list)
    # None means not stated; an empty list means the teacher stated there are none.
    languages: list[str] | None = None
    grouping: str | None = None
    stated_interests: list[str] = Field(default_factory=list)
    community_description: str | None = None


class DisciplineTargetStandards(BaseModel):
    """Field 2. Requested and confirmed alignments are kept apart."""

    subject: str | None = None
    jurisdiction: str = "IN"
    standards_version: str | None = None
    requested_standard_keys: list[str] = Field(default_factory=list)
    confirmed_standard_keys: list[str] = Field(default_factory=list)
    # Written by validate_plan, not by the teacher and not by the model.
    alignment_status: AlignmentStatus = AlignmentStatus.UNVERIFIED


class CommunityAssetsCulturalWealth(BaseModel):
    """Field 3. Empty means no asset was supplied, not that none exists."""

    asset_refs: list[str] = Field(default_factory=list)
    contributor_description: str | None = None
    cultural_wealth_tags: list[str] = Field(default_factory=list)
    local_applicability: str | None = None
    tag_note: str | None = (
        "Tags are optional and come from the contributor or a community review, "
        "never from a demographic inference."
    )


class ResearchGrounding(BaseModel):
    """Field 4. Curated defaults; teachers do not pick papers per lesson."""

    frameworks: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    teacher_preferences: list[str] = Field(default_factory=list)


class TopicSubject(BaseModel):
    """Field 5."""

    topic: str | None = None
    subject: str | None = None
    phenomenon_or_problem: str | None = None
    target_concept: str | None = None
    prerequisite_focus: str | None = None


class TeacherRawIntentions(BaseModel):
    """Field 6. The teacher's wording stays; the reading of it is correctable."""

    raw_text: str
    interpreted_goals: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)


class SeedLesson(BaseModel):
    """Field 7. Optional source material with its rights attached."""

    source_id: str | None = None
    permitted_excerpts: list[str] = Field(default_factory=list)
    editable_parts: list[str] = Field(default_factory=list)
    fixed_parts: list[str] = Field(default_factory=list)
    change_request: str | None = None
    rights: str | None = None
    lineage: str | None = None


class ClassroomResourceConstraints(BaseModel):
    """Field 8. Unknown resources are not unlimited resources."""

    available: list[str] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)
    quantities: dict[str, int] = Field(default_factory=dict)
    cost_limit: float | None = None
    space: str | None = None
    preparation_limit_minutes: int | None = None
    hard_constraints: list[str] = Field(default_factory=list)


class TimeBudget(BaseModel):
    """Field 9. Preparation and transitions are counted, not assumed away."""

    sessions: int | None = None
    minutes_per_session: int | None = None
    preparation_minutes_max: int | None = None
    transition_minutes: int | None = None

    @property
    def total_instructional_minutes(self) -> int | None:
        if self.sessions is None or self.minutes_per_session is None:
            return None
        return self.sessions * self.minutes_per_session


class OutputDeliverableScope(BaseModel):
    """Field 10. Completeness is judged against the request, not by length."""

    type: DeliverableScope = DeliverableScope.SINGLE_LESSON
    audience: str | None = None
    target_length: str | None = None
    include: list[str] = Field(default_factory=list)


class AssessmentPreference(BaseModel):
    """Field 11. A preferred mode must still elicit the intended learning."""

    purpose: AssessmentPurpose | None = None
    modes: list[str] = Field(default_factory=list)
    evidence_sought: str | None = None
    rubric_needed: bool = False
    accommodations: list[str] = Field(default_factory=list)


# Field numbers are the user's own; keep them stable across renames.
PLANNING_FIELDS: tuple[tuple[str, int], ...] = (
    ("classroom_community_profile", 1),
    ("discipline_target_standards", 2),
    ("community_assets_cultural_wealth", 3),
    ("research_grounding", 4),
    ("topic_subject", 5),
    ("teacher_raw_intentions", 6),
    ("seed_lesson", 7),
    ("classroom_resource_constraints", 8),
    ("time_budget", 9),
    ("output_deliverable_scope", 10),
    ("assessment_preference", 11),
)


class PlanningContext(BaseModel):
    """The eleven fields, versioned, each carrying its own status and origin."""

    schema_version: str = SCHEMA_VERSION
    context_id: str | None = None
    session_id: str | None = None
    created_at: datetime = Field(default_factory=_now)

    classroom_community_profile: Provenanced[ClassroomCommunityProfile] = Field(
        default_factory=Provenanced[ClassroomCommunityProfile])
    discipline_target_standards: Provenanced[DisciplineTargetStandards] = Field(
        default_factory=Provenanced[DisciplineTargetStandards])
    community_assets_cultural_wealth: Provenanced[CommunityAssetsCulturalWealth] = Field(
        default_factory=Provenanced[CommunityAssetsCulturalWealth])
    research_grounding: Provenanced[ResearchGrounding] = Field(
        default_factory=Provenanced[ResearchGrounding])
    topic_subject: Provenanced[TopicSubject] = Field(
        default_factory=Provenanced[TopicSubject])
    teacher_raw_intentions: Provenanced[TeacherRawIntentions] = Field(
        default_factory=Provenanced[TeacherRawIntentions])
    seed_lesson: Provenanced[SeedLesson] = Field(
        default_factory=Provenanced[SeedLesson])
    classroom_resource_constraints: Provenanced[ClassroomResourceConstraints] = Field(
        default_factory=Provenanced[ClassroomResourceConstraints])
    time_budget: Provenanced[TimeBudget] = Field(
        default_factory=Provenanced[TimeBudget])
    output_deliverable_scope: Provenanced[OutputDeliverableScope] = Field(
        default_factory=Provenanced[OutputDeliverableScope])
    assessment_preference: Provenanced[AssessmentPreference] = Field(
        default_factory=Provenanced[AssessmentPreference])

    def slots(self) -> list[tuple[str, int, Provenanced[Any]]]:
        return [(name, number, getattr(self, name))
                for name, number in PLANNING_FIELDS]

    def to_flat_export(self) -> dict[str, Any]:
        """The two-part shape used by the dataset examples.

        Values sit under ``planning_context`` and their status under a parallel
        ``field_state``, matching research/examples/planning_case.example.json
        so that exports stay readable next to the format already documented.
        """
        context: dict[str, Any] = {}
        state: dict[str, Any] = {}
        for name, number, slot in self.slots():
            value = slot.value
            context[name] = (value.model_dump(mode="json")
                             if isinstance(value, BaseModel) else value)
            state[name] = {
                "field_number": number,
                "status": slot.status.value,
                "origin": slot.origin.value if slot.origin else None,
                "teacher_confirmed": slot.teacher_confirmed,
            }
        return {"planning_context": context, "field_state": state}

    def to_prompt_view(self) -> dict[str, Any]:
        """What the model sees: known values, plus what is explicitly missing.

        The wrapper itself is not sent. Naming the unspecified fields is the
        point of this view, so that absence reads as absence rather than as an
        invitation to supply a plausible value.
        """
        supplied: dict[str, Any] = {}
        unspecified: dict[str, str] = {}
        for name, _number, slot in self.slots():
            if slot.is_known:
                value = slot.value
                supplied[name] = (value.model_dump(mode="json", exclude_none=True)
                                  if isinstance(value, BaseModel) else value)
            else:
                unspecified[name] = slot.status.value
        return {"supplied": supplied, "unspecified": unspecified}


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class StandardEvidence(BaseModel):
    """One standard, carrying the provenance the index build attached to it."""

    evidence_ref: str
    standard_key: str
    printed_code: str
    jurisdiction: str
    standards_version: str
    subject: str
    grade_or_course: str
    domain: str | None = None
    text: str
    clarification_statement: str | None = None
    science_dimensions: dict[str, str | None] | None = None
    source_url: str
    source_file: str
    page_number: int
    document_sha256: str
    extraction_review_status: str = "unreviewed"
    # True where the published document prints the same code more than once,
    # so the code alone does not identify one standard.
    standard_key_ambiguous: bool = False

    @classmethod
    def from_index_record(cls, record: dict[str, Any], evidence_ref: str) -> "StandardEvidence":
        """Build from a row of standards_index.json without restating its fields."""
        return cls(
            evidence_ref=evidence_ref,
            standard_key=record["standard_key"],
            printed_code=record["printed_code"],
            jurisdiction=record["jurisdiction"],
            standards_version=record["standards_version"],
            subject=record["subject"],
            grade_or_course=record["grade_or_course"],
            domain=record.get("domain"),
            text=record["description"],
            clarification_statement=record.get("clarification_statement"),
            science_dimensions=record.get("science_dimensions"),
            source_url=record["source_url"],
            source_file=record["source_file"],
            page_number=record["page_number"],
            document_sha256=record["document_sha256"],
            extraction_review_status=record.get("extraction_review_status", "unreviewed"),
            standard_key_ambiguous=record.get("standard_key_ambiguous", False),
        )


class ResearchEvidence(BaseModel):
    """A short pedagogy record. A framework name is not an effect size."""

    evidence_ref: str
    framework: str
    framework_version: str | None = None
    claim: str
    citation: str
    evidence_type: str | None = None
    population_context: str | None = None
    limitations: str | None = None


class AssetPermissions(BaseModel):
    """Four separate permissions. One must never stand in for another."""

    classroom_generation: bool = False
    research_analysis: bool = False
    model_training: bool = False
    public_sharing: bool = False


class CommunityEvidence(BaseModel):
    """A contributed asset, in the contributor's own words."""

    evidence_ref: str
    asset_id: str
    version: str | None = None
    contributor_description: str
    community_authority: str | None = None
    suitable_contexts: list[str] = Field(default_factory=list)
    usage_restrictions: list[str] = Field(default_factory=list)
    permissions: AssetPermissions = Field(default_factory=AssetPermissions)
    attribution_required: str | None = None
    cultural_wealth_tags: list[str] = Field(default_factory=list)
    reviewed_on: datetime | None = None
    expires_on: datetime | None = None


class SeedExcerpt(BaseModel):
    """A permitted extract from a source lesson, with its lineage."""

    evidence_ref: str
    source_id: str
    excerpt: str
    rights: str | None = None
    lineage: str | None = None


class EvidenceBundle(BaseModel):
    """Exactly what was put in front of the model, kept for later replay."""

    schema_version: str = SCHEMA_VERSION
    bundle_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    retrieval_config: dict[str, Any] = Field(default_factory=dict)
    standards: list[StandardEvidence] = Field(default_factory=list)
    research: list[ResearchEvidence] = Field(default_factory=list)
    community: list[CommunityEvidence] = Field(default_factory=list)
    seed_excerpts: list[SeedExcerpt] = Field(default_factory=list)

    def refs(self) -> set[str]:
        """Every evidence id a plan is allowed to cite."""
        return {item.evidence_ref for group in
                (self.standards, self.research, self.community, self.seed_excerpts)
                for item in group}

    def standard_keys(self) -> set[str]:
        return {s.standard_key for s in self.standards}

    def community_by_ref(self) -> dict[str, CommunityEvidence]:
        return {c.evidence_ref: c for c in self.community}


# ---------------------------------------------------------------------------
# Lesson plan
# ---------------------------------------------------------------------------


class AlignmentRecord(BaseModel):
    """Standard to goal to activity to assessment, so coverage is inspectable.

    A correct code proves nothing on its own; this record is what makes the
    claim checkable by a reviewer.
    """

    standard_key: str
    evidence_ref: str
    learning_goal: str
    activity_evidence: str
    assessment_evidence: str
    coverage: Coverage


class AccessSupport(BaseModel):
    """Barrier to option to the goal the option preserves."""

    barrier: str
    design_option: str
    retained_learning_goal: str


class CommunityUse(BaseModel):
    """How a supplied asset shapes the task, and what the student may choose."""

    evidence_ref: str
    use_in_task: str
    student_choice: str
    attribution: str


class SequenceStep(BaseModel):
    name: str
    minutes: int
    description: str
    grouping: str | None = None
    materials: list[str] = Field(default_factory=list)


class AssessmentSpec(BaseModel):
    purpose: AssessmentPurpose
    prompt: str
    expected_evidence: str
    criteria: list[str] = Field(default_factory=list)
    response_options: list[str] = Field(default_factory=list)


class LessonPlanDraft(BaseModel):
    """What the model is allowed to produce.

    Deliberately excludes ``alignment_status``, ``validation`` and every
    identifier. Those are the application's to set. Keeping them out of the
    type is what stops a fluent draft from declaring itself verified.
    """

    title: str
    objectives: list[str] = Field(default_factory=list)
    alignment: list[AlignmentRecord] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    sequence: list[SequenceStep] = Field(default_factory=list)
    access_options: list[AccessSupport] = Field(default_factory=list)
    assessment: AssessmentSpec | None = None
    community_use: list[CommunityUse] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class LessonPlan(LessonPlanDraft):
    """A draft once the application has stamped and checked it."""

    schema_version: str = SCHEMA_VERSION
    plan_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    alignment_status: AlignmentStatus = AlignmentStatus.UNVERIFIED
    validation: "ValidationReport | None" = None

    @classmethod
    def promote(cls, draft: LessonPlanDraft, *, plan_id: str | None = None) -> "LessonPlan":
        """Turn a model draft into a plan. Status stays unverified until checked."""
        return cls(**draft.model_dump(), plan_id=plan_id)


# ---------------------------------------------------------------------------
# Validation results
# ---------------------------------------------------------------------------


class ValidationFinding(BaseModel):
    code: str
    severity: Severity
    message: str
    path: str | None = None


class ValidationReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    checked_at: datetime = Field(default_factory=_now)
    findings: list[ValidationFinding] = Field(default_factory=list)
    alignment_status: AlignmentStatus = AlignmentStatus.UNVERIFIED

    @property
    def errors(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def passed(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Events
#
# These are the immutable record of what happened, written once and never
# edited. SFT and DPO exports are generated from them, which is why each one
# stores the exact inputs rather than a reference to a corpus that may change.
# ---------------------------------------------------------------------------


class ContextSnapshot(BaseModel):
    event_id: str
    session_id: str
    created_at: datetime = Field(default_factory=_now)
    schema_version: str = SCHEMA_VERSION
    raw_request: str
    context: PlanningContext


class EvidenceSnapshot(BaseModel):
    event_id: str
    session_id: str
    created_at: datetime = Field(default_factory=_now)
    schema_version: str = SCHEMA_VERSION
    bundle: EvidenceBundle
    standards_index_sha256: str | None = None


class GenerationEvent(BaseModel):
    event_id: str
    session_id: str
    created_at: datetime = Field(default_factory=_now)
    schema_version: str = SCHEMA_VERSION
    context_snapshot_id: str
    evidence_snapshot_id: str
    # The messages exactly as sent, so the prompt can be rebuilt byte for byte.
    prompt_messages: list[dict[str, str]] = Field(default_factory=list)
    prompt_version: str
    model: str
    model_options: dict[str, Any] = Field(default_factory=dict)
    output_raw: str
    plan: LessonPlan | None = None
    validation: ValidationReport | None = None


class RubricRating(BaseModel):
    dimension: RubricDimension
    score: int | None = Field(default=None, ge=0, le=3)
    not_applicable_reason: str | None = None
    rater_role: str | None = None
    rubric_version: str | None = None
    critical_flags: list[CriticalFlag] = Field(default_factory=list)


class RevisionEvent(BaseModel):
    event_id: str
    created_at: datetime = Field(default_factory=_now)
    schema_version: str = SCHEMA_VERSION
    generation_id: str
    edited_plan: LessonPlan
    # A teacher who adds a fact while editing has changed the inputs. The edit
    # then belongs to a new context, not to the prompt that came before it.
    introduced_new_requirements: bool = False
    new_context_snapshot_id: str | None = None
    review_status: str = "unreviewed"
    ratings: list[RubricRating] = Field(default_factory=list)
    reviewer_notes: str | None = None


class PreferenceEvent(BaseModel):
    event_id: str
    created_at: datetime = Field(default_factory=_now)
    schema_version: str = SCHEMA_VERSION
    # Both candidates must answer the same inputs, or the pair teaches nothing.
    context_snapshot_id: str
    evidence_snapshot_id: str
    candidate_a_generation_id: str
    candidate_b_generation_id: str
    label: PreferenceLabel
    reviewer_id: str | None = None
    reviewer_role: str | None = None
    decisive_dimensions: list[RubricDimension] = Field(default_factory=list)
    rationale: str | None = None
    confidence: int | None = Field(default=None, ge=1, le=5)
    adjudication_status: str = "none"


class ExportExclusion(BaseModel):
    event_id: str
    reason: str


class ExportManifest(BaseModel):
    export_id: str
    dataset_version: str
    created_at: datetime = Field(default_factory=_now)
    schema_version: str = SCHEMA_VERSION
    included_event_ids: list[str] = Field(default_factory=list)
    # Related prompts, variants and revisions share one split, assigned at the
    # lesson-family level so a paraphrase cannot leak across it.
    split_assignments: dict[str, str] = Field(default_factory=dict)
    preprocessing_versions: dict[str, str] = Field(default_factory=dict)
    exclusions: list[ExportExclusion] = Field(default_factory=list)


LessonPlan.model_rebuild()
