"""Persist diagnostic evidence atomically with evaluation execution history."""

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_asr_dependency import adapters, claim

from app.evaluation.models import EvaluationDiagnosticEvidence, EvaluationJob, RawEvaluation
from app.evaluation.worker import KnownFailure, authorize_retry, execute_claim
from app.scoring.models import FinalScoreResult
from app.scoring.worker import run_settlement_once

pytest_plugins = ("test_asr_dependency",)


def test_diagnostic_evidence_is_immutable_and_retained_across_retry(db, dependency_flow):
    flow = dependency_flow()
    asr, _, _, sources = adapters(flow)
    number = 0

    def reject(inputs, requirement):
        nonlocal number
        number += 1
        raise KnownFailure(
            "ISE_REJECTED", diagnostic={"except_info": str(number), "xml": "<result/>"}
        )

    asr.evaluate = reject
    job_id = flow["jobs"]["test-asr"]
    assert execute_claim(db, claim(db, flow, "test-asr", sources))
    assert not run_settlement_once(db, registry=flow["registry"])["completed"]
    with Session(db) as session, session.begin():
        authorize_retry(session, job_id)
    assert execute_claim(db, claim(db, flow, "test-asr", sources))
    with Session(db) as session:
        rows = list(
            session.scalars(
                select(RawEvaluation)
                .where(RawEvaluation.job_id == job_id)
                .order_by(RawEvaluation.created_at)
            )
        )
        assert len(rows) == 2 and all(
            row.status == "FAILED" and row.payload is None for row in rows
        )
        assert rows[0].execution_id != rows[1].execution_id
        evidence = [
            session.get(EvaluationDiagnosticEvidence, row.evaluation_id).evidence for row in rows
        ]
        assert [item["except_info"] for item in evidence] == ["1", "2"]
        assert session.get(EvaluationJob, job_id).status == "FAILED"
        assert session.scalar(select(FinalScoreResult)) is None
        identity = rows[0].evaluation_id
    for statement in (
        "UPDATE evaluation_diagnostic_evidence SET evidence='{}' WHERE evaluation_id=:id",
        "DELETE FROM evaluation_diagnostic_evidence WHERE evaluation_id=:id",
    ):
        with pytest.raises(DBAPIError, match="immutable"), db.begin() as connection:
            connection.execute(text(statement), {"id": identity})


def test_diagnostic_failure_rolls_back_raw_and_job_together(db, dependency_flow):
    flow = dependency_flow()
    asr, _, _, sources = adapters(flow)

    def reject(inputs, requirement):
        raise KnownFailure("ISE_REJECTED", diagnostic={"xml": "<result/>"})

    asr.evaluate = reject
    work = claim(db, flow, "test-asr", sources)

    def reject_insert(mapper, connection, target):
        raise RuntimeError("simulated evidence storage failure")

    event.listen(EvaluationDiagnosticEvidence, "before_insert", reject_insert)
    try:
        with pytest.raises(RuntimeError, match="simulated evidence"):
            execute_claim(db, work)
    finally:
        event.remove(EvaluationDiagnosticEvidence, "before_insert", reject_insert)
    with Session(db) as session:
        assert (
            session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == work.job_id)) is None
        )
        job = session.get(EvaluationJob, work.job_id)
        assert job.status == "RUNNING" and job.claim_token == work.token


def test_late_rejection_does_not_write_diagnostic(db, dependency_flow):
    flow = dependency_flow()
    asr, _, _, sources = adapters(flow)
    work = claim(db, flow, "test-asr", sources)

    def reject_late(inputs, requirement):
        with db.begin() as connection:
            connection.execute(
                text(
                    "UPDATE evaluation_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=:id"
                ),
                {"id": work.job_id},
            )
        raise KnownFailure("ISE_REJECTED", diagnostic={"xml": "<result/>"})

    asr.evaluate = reject_late
    assert not execute_claim(db, work)
    with Session(db) as session:
        assert (
            session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == work.job_id)) is None
        )
        assert session.scalar(select(EvaluationDiagnosticEvidence)) is None


@pytest.mark.db_isolated
def test_downgrade_refuses_to_discard_diagnostic_history(db, dependency_flow):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    flow = dependency_flow()
    asr, _, _, sources = adapters(flow)

    def reject(inputs, requirement):
        raise KnownFailure("ISE_REJECTED", diagnostic={"xml": "<result/>"})

    asr.evaluate = reject
    assert execute_claim(db, claim(db, flow, "test-asr", sources))
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(DBAPIError, match="cannot discard evaluation diagnostic history"):
            command.downgrade(config, "0027_auth_users_sessions")
        connection.rollback()
