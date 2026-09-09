"""Checks that compare a plan against the context and evidence it came from.

These cannot live on the models. A ``LessonPlan`` does not know the teacher's
time budget, and an ``AlignmentRecord`` does not know which standards were
actually retrieved, so the interesting failures are only visible with all three
objects in hand.

Nothing here judges teaching quality. It establishes that the plan is internally
consistent, cites only evidence that was supplied, and respects the constraints
and permissions it was given. A plan can pass every check and still be a poor
lesson; that is what the rubric and human review are for.
"""

from collections import Counter

from .schemas import (
    SourceVerificationStatus,
    EvidenceBundle,
    LessonPlan,
    PlanningContext,
    Severity,
    ValidationFinding,
    ValidationReport,
)


def _finding(code: str, severity: Severity, message: str,
             path: str | None = None) -> ValidationFinding:
    return ValidationFinding(code=code, severity=severity, message=message, path=path)


def validate_plan(plan: LessonPlan, context: PlanningContext,
                  evidence: EvidenceBundle) -> ValidationReport:
    """Check one plan and return what is wrong with it, source verification separately from pedagogical alignment."""
    findings: list[ValidationFinding] = []

    allowed_refs = evidence.refs()
    allowed_keys = evidence.standard_keys()
    refs = [item.evidence_ref for group in
            (evidence.standards, evidence.research, evidence.community, evidence.seed_excerpts)
            for item in group]
    duplicate_refs = {ref for ref, count in Counter(refs).items() if count > 1}
    for ref in sorted(duplicate_refs):
        findings.append(_finding(
            "duplicate_evidence_ref", Severity.ERROR,
            f"Evidence ref {ref!r} identifies more than one supplied record.", "evidence"))
    standards_by_ref = {s.evidence_ref: s for s in evidence.standards
                        if s.evidence_ref not in duplicate_refs}
    key_counts = Counter(s.standard_key for s in evidence.standards)
    community_by_ref = evidence.community_by_ref()

    # --- citations resolve -------------------------------------------------
    for i, record in enumerate(plan.alignment):
        path = f"alignment[{i}]"
        if record.standard_key not in allowed_keys:
            findings.append(_finding(
                "standard_not_in_evidence", Severity.ERROR,
                f"Cites {record.standard_key}, which was not retrieved for this "
                f"request. A standard the model was not shown cannot be cited.",
                path))
        if record.evidence_ref not in allowed_refs:
            findings.append(_finding(
                "evidence_ref_not_found", Severity.ERROR,
                f"Cites evidence {record.evidence_ref!r}, which is not in the bundle.",
                path))
        standard = standards_by_ref.get(record.evidence_ref)
        if record.evidence_ref in allowed_refs and (
            standard is None or standard.standard_key != record.standard_key
        ):
            findings.append(_finding(
                "standard_evidence_mismatch", Severity.ERROR,
                f"Evidence {record.evidence_ref!r} does not uniquely identify "
                f"standard {record.standard_key}.", path))
        if standard is not None and (standard.standard_key_ambiguous
                                     or key_counts[standard.standard_key] > 1):
            findings.append(_finding(
                "ambiguous_standard_key", Severity.WARNING,
                f"{record.standard_key} does not uniquely identify a source standard. "
                "Keep the domain and evidence ref; resolve the source ambiguity "
                "before marking the source verified.", path))

    for i, ref in enumerate(plan.citations):
        if ref not in allowed_refs:
            findings.append(_finding(
                "evidence_ref_not_found", Severity.ERROR,
                f"Cites evidence {ref!r}, which is not in the bundle.",
                f"citations[{i}]"))

    # --- community assets --------------------------------------------------
    for i, use in enumerate(plan.community_use):
        path = f"community_use[{i}]"
        asset = community_by_ref.get(use.evidence_ref)
        if asset is None:
            findings.append(_finding(
                "community_asset_not_found", Severity.ERROR,
                f"Uses community evidence {use.evidence_ref!r}, which is not in "
                f"the bundle. A community fact must never be invented.",
                path))
            continue
        if not asset.permissions.classroom_generation:
            findings.append(_finding(
                "community_use_not_permitted", Severity.ERROR,
                f"Asset {asset.asset_id} has not been cleared for classroom "
                f"generation.",
                path))
        if asset.attribution_required and not use.attribution.strip():
            findings.append(_finding(
                "attribution_missing", Severity.ERROR,
                f"Asset {asset.asset_id} requires attribution and none is given.",
                path))
        for restriction in asset.usage_restrictions:
            findings.append(_finding(
                "community_restriction_to_check", Severity.INFO,
                f"Asset {asset.asset_id} carries a restriction that a person "
                f"must check: {restriction}",
                path))

    # --- timing ------------------------------------------------------------
    planned = sum(step.minutes for step in plan.sequence)
    budget = context.time_budget.value if context.time_budget.is_known else None
    if budget is not None:
        available = budget.total_instructional_minutes
        if available is not None and planned > available:
            findings.append(_finding(
                "over_time_budget", Severity.ERROR,
                f"The sequence needs {planned} minutes but the budget is "
                f"{available}. Transitions and setup are not counted here.",
                "sequence"))
        elif available is not None and planned < available * 0.6:
            findings.append(_finding(
                "well_under_time_budget", Severity.WARNING,
                f"The sequence fills {planned} of {available} minutes. Check "
                f"whether a section is missing.",
                "sequence"))
    elif plan.sequence:
        findings.append(_finding(
            "time_budget_unknown", Severity.INFO,
            f"The sequence claims {planned} minutes, but no time budget was "
            f"supplied to check it against.",
            "sequence"))

    for i, step in enumerate(plan.sequence):
        if step.minutes <= 0:
            findings.append(_finding(
                "step_without_duration", Severity.ERROR,
                f"Step {step.name!r} has no positive duration.",
                f"sequence[{i}]"))

    # --- resources ---------------------------------------------------------
    constraints = (context.classroom_resource_constraints.value
                   if context.classroom_resource_constraints.is_known else None)
    if constraints is not None:
        unavailable = {item.strip().lower() for item in constraints.unavailable}
        wanted = {(m, "materials") for m in plan.materials}
        wanted |= {(m, f"sequence[{i}]")
                   for i, step in enumerate(plan.sequence) for m in step.materials}
        for material, path in sorted(wanted):
            if material.strip().lower() in unavailable:
                findings.append(_finding(
                    "material_unavailable", Severity.ERROR,
                    f"The plan needs {material!r}, which the teacher listed as "
                    f"unavailable.",
                    path))
        prep_cap = constraints.preparation_limit_minutes
        if prep_cap is not None and budget is not None:
            prep = budget.preparation_minutes_max
            if prep is not None and prep > prep_cap:
                findings.append(_finding(
                    "preparation_over_limit", Severity.ERROR,
                    f"Preparation of {prep} minutes exceeds the stated limit of "
                    f"{prep_cap}.",
                    "time_budget"))

    # --- completeness ------------------------------------------------------
    if not plan.objectives:
        findings.append(_finding(
            "no_objectives", Severity.ERROR,
            "The plan states no learning objectives.", "objectives"))

    if plan.objectives and not plan.alignment:
        findings.append(_finding(
            "no_alignment_records", Severity.WARNING,
            "No objective is linked to a standard, so coverage cannot be checked.",
            "alignment"))

    for i, record in enumerate(plan.alignment):
        if record.coverage.value == "not_addressed":
            findings.append(_finding(
                "standard_not_addressed", Severity.WARNING,
                f"{record.standard_key} is listed but marked not addressed.",
                f"alignment[{i}]"))

    target = (context.discipline_target_standards.value
              if context.discipline_target_standards.is_known else None)
    if target is not None:
        covered = {r.standard_key for r in plan.alignment}
        for key in target.confirmed_standard_keys:
            if key not in covered:
                findings.append(_finding(
                    "confirmed_standard_uncovered", Severity.WARNING,
                    f"The teacher confirmed {key} but no alignment record "
                    f"addresses it.",
                    "alignment"))

    scope = (context.output_deliverable_scope.value
             if context.output_deliverable_scope.is_known else None)
    if scope is not None and "assessment" in {s.lower() for s in scope.include} \
            and plan.assessment is None:
        findings.append(_finding(
            "requested_section_missing", Severity.WARNING,
            "An assessment was requested but the plan does not include one.",
            "assessment"))

    access_needs = (context.classroom_community_profile.value.access_needs
                    if context.classroom_community_profile.is_known else [])
    if access_needs and not plan.access_options:
        findings.append(_finding(
            "access_needs_unaddressed", Severity.ERROR,
            f"{len(access_needs)} access need(s) were stated and the plan offers "
            f"no access options.",
            "access_options"))

    report = ValidationReport(findings=findings)
    report.source_verification_status = _source_verification_status(plan, evidence, report)
    return report


def _source_verification_status(plan: LessonPlan, evidence: EvidenceBundle,
                                report: ValidationReport) -> SourceVerificationStatus:
    """Verify source identity and review only; assess teaching quality separately."""
    binding_errors = {"duplicate_evidence_ref", "standard_not_in_evidence",
                      "evidence_ref_not_found", "standard_evidence_mismatch"}
    if not plan.alignment or any(f.code in binding_errors for f in report.errors):
        return SourceVerificationStatus.UNVERIFIED
    standards_by_ref = {s.evidence_ref: s for s in evidence.standards}
    cited = [standards_by_ref[r.evidence_ref] for r in plan.alignment]
    ambiguous = any(f.code == "ambiguous_standard_key" for f in report.findings)
    if not ambiguous and all(s.extraction_review_status == "reviewed" for s in cited):
        return SourceVerificationStatus.VERIFIED
    return SourceVerificationStatus.PROVISIONAL
