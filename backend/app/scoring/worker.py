"""Advance closed sessions from durable settlement intents in independent transactions."""

from traceback import extract_tb

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.scoring.registry import PRODUCTION_REGISTRY, Registry
from app.scoring.settlement import diagnose_review_policy, settle_session
from app.sessions.assessment_session import AssessmentSession
from app.sessions.close_manifest import CloseManifest, SettlementIntent


def run_settlement_once(
    engine: Engine, *, registry: Registry = PRODUCTION_REGISTRY, batch_size: int = 100
) -> dict:
    """Visit each eligible session once, paging IDs without starving waiting sessions.

    Each candidate gets its own transaction. A locked session is skipped until a
    later scan. Failures are reported by session, exception type and error location, without
    exposing potentially sensitive exception messages or treating them as pending.
    """
    if type(batch_size) is not int or batch_size <= 0:
        raise ValueError("Batch size must be positive")
    result = {"completed": [], "pending": [], "skipped_locked": [], "failures": []}
    cursor = None
    while True:
        with Session(engine) as db:
            query = (
                select(AssessmentSession.session_id)
                .join(CloseManifest, CloseManifest.session_id == AssessmentSession.session_id)
                .join(SettlementIntent, SettlementIntent.manifest_id == CloseManifest.manifest_id)
                .where(AssessmentSession.status == "INPUT_CLOSED")
                .order_by(AssessmentSession.session_id)
                .limit(batch_size)
            )
            if cursor is not None:
                query = query.where(AssessmentSession.session_id > cursor)
            ids = list(db.scalars(query))
        if not ids:
            break
        cursor = ids[-1]
        for session_id in ids:
            try:
                with Session(engine) as db, db.begin():
                    db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                    owner = db.scalar(
                        select(AssessmentSession)
                        .where(AssessmentSession.session_id == session_id)
                        .with_for_update(skip_locked=True)
                        .execution_options(populate_existing=True)
                    )
                    if owner is None:
                        result["skipped_locked"].append(session_id)
                        continue
                    if owner.status != "INPUT_CLOSED":
                        continue
                    final = settle_session(db, session_id=session_id, registry=registry)
                    if final is None:
                        diagnostic = diagnose_review_policy(
                            db, session_id=session_id, registry=registry
                        )
                        outcome = ("pending", {"session_id": session_id, "diagnostic": diagnostic})
                    else:
                        outcome = (
                            "completed",
                            {"session_id": session_id, "result_id": final.result_id},
                        )
                result[outcome[0]].append(outcome[1])
            except Exception as exc:  # noqa: BLE001 - isolate and report each failed transaction
                frame = extract_tb(exc.__traceback__)[-1]
                result["failures"].append(
                    {
                        "session_id": session_id,
                        "error_type": type(exc).__name__,
                        "location": {
                            "file": frame.filename,
                            "line": frame.lineno,
                            "function": frame.name,
                        },
                    }
                )
    return result
