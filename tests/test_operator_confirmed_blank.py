"""Safety contracts for explicit operator-confirmed blank form fields."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from core.submission_domain import (
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    FieldType,
    FormFieldConstraintsV1,
    FormFieldV1,
    FormPlanV1,
    SensitiveCategory,
)
from core.submission_service import reconstruct_persisted_form_plan
from db.models import Application, Base, FormPlan, Job, JobStatus, OperatorApprovedAnswer


def _field(**overrides) -> FormFieldV1:
    values = {
        "field_id": "current-company",
        "label": "Current company",
        "field_type": FieldType.TEXT,
        "required": False,
        "position": 0,
    }
    values.update(overrides)
    return FormFieldV1(**values)


def _plan(field: FormFieldV1, decision: AnswerDecisionV1) -> FormPlanV1:
    now = datetime.now(UTC)
    return FormPlanV1(
        plan_id=uuid4(),
        application_id=1,
        application_revision=1,
        adapter_name="lever",
        adapter_version="1.0.0",
        selector_version="v4",
        form_fingerprint="f" * 64,
        selected_cv_id="cv-ai",
        selected_cv_hash="a" * 64,
        attached_cv_id="cv-ai",
        attached_cv_hash="a" * 64,
        attachment_verified=True,
        profile_version=1,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        fields=(field,),
        decisions=(decision,),
    )


def _blank_decision(field_id: str = "current-company") -> AnswerDecisionV1:
    return AnswerDecisionV1(
        field_id=field_id,
        disposition=AnswerDisposition.OPERATOR_CONFIRMED_BLANK,
        provenance=AnswerProvenance.OPERATOR_CONFIRMED,
        evidence_refs=("operator_confirmation:dashboard_review",),
        confidence=1.0,
    )


def test_optional_non_sensitive_blank_is_auditable_and_ready() -> None:
    plan = _plan(_field(), _blank_decision())

    assert plan.ready_for_permit is True
    assert plan.decisions[0].value is None
    assert plan.decisions[0].disposition is AnswerDisposition.OPERATOR_CONFIRMED_BLANK


@pytest.mark.parametrize(
    "field",
    [
        _field(required=True),
        _field(sensitive_category=SensitiveCategory.DEMOGRAPHIC),
        _field(constraints=FormFieldConstraintsV1(min_length=1)),
        _field(field_type=FieldType.FILE),
        _field(field_type=FieldType.CONSENT, sensitive_category=SensitiveCategory.CONSENT),
    ],
)
def test_blank_confirmation_cannot_bypass_required_sensitive_or_nonblank_controls(
    field: FormFieldV1,
) -> None:
    with pytest.raises(ValidationError):
        _plan(field, _blank_decision(field.field_id))


def test_blank_confirmation_requires_operator_evidence_and_provenance() -> None:
    with pytest.raises(ValidationError):
        AnswerDecisionV1(
            field_id="current-company",
            disposition=AnswerDisposition.OPERATOR_CONFIRMED_BLANK,
            provenance=AnswerProvenance.USER_CONFIRMED,
        )

    with pytest.raises(ValidationError):
        AnswerDecisionV1(
            field_id="current-company",
            disposition=AnswerDisposition.OPERATOR_CONFIRMED_BLANK,
            provenance=AnswerProvenance.OPERATOR_CONFIRMED,
            evidence_refs=("profile:user_confirmed:current_company",),
        )


@pytest.mark.asyncio
async def test_api_confirm_blank_clones_plan_without_creating_reusable_fact(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'blank-confirmation.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = datetime.now(UTC).replace(tzinfo=None)
    job = Job(
        title="Engineer",
        company="Example",
        source_url="https://example.test/jobs/1",
        apply_url="https://example.test/jobs/1",
        status=JobStatus.DRAFT,
    )
    db.add(job)
    db.flush()
    application = Application(
        job_id=job.id,
        status=JobStatus.DRAFT,
        revision=1,
        selected_cv_id="cv-ai",
        selected_cv_hash="a" * 64,
        profile_version=1,
    )
    db.add(application)
    db.flush()
    plan = FormPlan(
        plan_id=str(uuid4()),
        application_id=application.id,
        application_revision=1,
        adapter_name="lever",
        adapter_version="1.0.0",
        selector_version="v4",
        fingerprint="f" * 64,
        selected_cv_id="cv-ai",
        selected_cv_hash="a" * 64,
        attached_cv_id="cv-ai",
        attached_cv_hash="a" * 64,
        attachment_verified=True,
        profile_version=1,
        fields_json=(
            '[{"field_id":"current-company","label":"Current company",'
            '"field_type":"text","required":false,"position":0}]'
        ),
        decisions_json=(
            '[{"field_id":"current-company","disposition":"operator_required",'
            '"provenance":"abstained","reason_code":"REQUIRED_FIELD_UNKNOWN"}]'
        ),
        blockers_json="[]",
        locale="en",
        answer_policy_version="answer-policy-v1",
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(plan)
    db.commit()

    result = await applications_route.confirm_application_answer(
        application.id,
        "current-company",
        applications_route.ConfirmAnswerRequest(
            plan_id=plan.plan_id,
            application_revision=1,
            confirm_blank=True,
            evidence_source="operator_confirmation",
            evidence_reference="dashboard_review",
        ),
        db,
    )

    cloned = (
        db.query(FormPlan)
        .filter(FormPlan.application_id == application.id)
        .order_by(FormPlan.id.desc())
        .first()
    )
    assert cloned is not None
    domain = reconstruct_persisted_form_plan(cloned)
    assert domain.decisions[0].disposition is AnswerDisposition.OPERATOR_CONFIRMED_BLANK
    assert domain.decisions[0].provenance is AnswerProvenance.OPERATOR_CONFIRMED
    assert domain.blockers == ()
    assert result.valid is False
    assert db.query(OperatorApprovedAnswer).count() == 0
    db.close()
