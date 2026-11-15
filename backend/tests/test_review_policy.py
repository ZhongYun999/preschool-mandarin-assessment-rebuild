"""Frozen review policy and sampling integration on PostgreSQL."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_score_review import decide
from test_score_settlement import prepare

from app.scoring.models import PendingReview, ReviewDecision
from app.scoring.registry import EvaluationContract, Registry
from app.scoring.review import record_review
from app.scoring.review_policy import PolicyUndetermined, sample_task, validate_policy
from app.scoring.settlement import diagnose_review_policy, settle_session
from app.sessions.assessment_session import AssessmentSession
from app.sessions.close_manifest import manifest_entries

pytest_plugins = ("test_score_review",)

SID = UUID("00112233-4455-6677-8899-aabbccddeeff")
TID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
EID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def sample(policy, task=TID, entry=EID, kind="qa"):
    return sample_task(
        policy,
        session_id=SID,
        session_task_id=task,
        paper_entry_id=entry,
        task_type=kind,
        task_types={"qa", "dialogue"},
        paper_entry_ids={str(EID)},
    )


def test_boundaries_priority_and_stability():
    assert not sample({"version": "v1", "default_ratio": 0}).sampled
    assert sample({"version": "v1", "default_ratio": 1}).sampled
    policy = {
        "version": "v1",
        "default_ratio": 0.25,
        "task_type_ratios": {"qa": 0.5},
        "paper_entry_ratios": {str(EID): 1},
    }
    result = sample(policy)
    assert result.sampled and result.source == "paper_entry" and result.ratio == 1
    del policy["paper_entry_ratios"]
    assert sample(policy).source == "task_type" and sample(policy).ratio == 0.5
    del policy["task_type_ratios"]
    expected = sample(policy)
    assert expected.bucket == "9832459463e2014d6fec1ef44049f71607593d7965066c5191c6947043b149f2"
    assert expected.source == "default" and all(sample(policy) == expected for _ in range(20))
    n, d = expected.ratio.as_integer_ratio()
    assert expected.sampled == (int(expected.bucket, 16) * d < n * 2**256)
    assert not sample({"sampling": 0}).sampled


def test_process_and_order_independent():
    policy = {"version": "v1", "default_ratio": 0.25}
    expected = sample(policy)
    code = (
        "from uuid import UUID; from app.scoring.review_policy import sample_task; "
        "r=sample_task({'version':'v1','default_ratio':0.25},session_id=UUID('"
        + str(SID)
        + "'),session_task_id=UUID('"
        + str(TID)
        + "'),paper_entry_id=UUID('"
        + str(EID)
        + "'),task_type='qa',task_types={'qa'},paper_entry_ids={'"
        + str(EID)
        + "'}); print(r.bucket, r.sampled)"
    )
    env = dict(os.environ, PYTHONPATH="backend")
    output = subprocess.check_output([sys.executable, "-c", code], env=env, text=True).strip()
    assert output == f"{expected.bucket} {expected.sampled}"
    ids = [UUID(int=i) for i in range(1, 11)]
    forward = {key: sample(policy, task=key) for key in ids}
    assert forward == {key: sample(policy, task=key) for key in reversed(ids)}


@pytest.mark.parametrize(
    "policy",
    [
        {"sampling": 0.25},
        {"version": "v2", "default_ratio": 0},
        {"version": "v1"},
        {"version": "v1", "default_ratio": True},
        {"version": "v1", "default_ratio": -0.01},
        {"version": "v1", "default_ratio": 1.01},
        {"version": "v1", "default_ratio": 10**400},
        {"version": "v1", "default_ratio": float("nan")},
        {"version": "v1", "default_ratio": 0, "extra": 0},
        {"version": "v1", "default_ratio": 0, "task_type_ratios": {"unknown": 0}},
        {"version": "v1", "default_ratio": 0, "paper_entry_ratios": {str(SID): 0}},
    ],
)
def test_invalid_policies_diagnostic(policy):
    with pytest.raises(PolicyUndetermined):
        validate_policy(policy, task_types={"qa"}, paper_entry_ids={str(EID)})


def test_source_declares_no_recommendation():
    contract = EvaluationContract("test", "1", frozenset({"qa"}), lambda _: True, False)
    registry = Registry([], [contract])
    assert (
        registry.review_recommendation(
            {"source": "test", "version": "1"}, {"review_recommended": True}
        )
        is False
    )
    assert (
        Registry(
            [], [EvaluationContract("test", "1", frozenset({"qa"}), lambda _: True)]
        ).review_recommendation({"source": "test", "version": "1"}, {})
        is None
    )


def test_sampled_tasks_require_review_and_snapshot_immutable(db, two_tasks, tmp_path):
    policy = {"version": "v1", "default_ratio": 1}
    sid, mid, plugin, registry = prepare(db, two_tasks, tmp_path, policy)
    policy["default_ratio"] = 0
    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=sid, registry=registry) is None
        entries = [(e.session_task_id, e.submission_id) for e in manifest_entries(s, mid)]
        assert s.get(AssessmentSession, sid).review_policy["default_ratio"] == 1
        assert [r.reason for r in s.scalars(select(PendingReview)).all()] == ["SAMPLED", "SAMPLED"]
    with Session(db) as s, s.begin():
        record_review(
            s,
            session_id=sid,
            session_task_id=entries[0][0],
            submission_id=entries[0][1],
            operation_id="one",
            teacher_id="t",
            expected_revision=0,
            action="CONFIRM",
            registry=registry,
        )
        assert settle_session(s, session_id=sid, registry=registry) is None
    with Session(db) as s, s.begin():
        record_review(
            s,
            session_id=sid,
            session_task_id=entries[1][0],
            submission_id=entries[1][1],
            operation_id="two",
            teacher_id="t",
            expected_revision=1,
            action="CONFIRM",
            registry=registry,
        )
        assert settle_session(s, session_id=sid, registry=registry)
    assert plugin.calls == ["task", "task", "paper", "paper", "paper"]


@pytest.mark.parametrize("ready_review", [{"version": "v1", "default_ratio": 1}], indirect=True)
def test_combined_reason_and_sampled_override(db, ready_review):
    c = ready_review
    with Session(db) as s:
        reasons = {r.session_task_id: r.reason for r in s.scalars(select(PendingReview))}
        assert reasons[c["contexts"][0]["session_task_id"]] == "SAMPLED+MODEL_RECOMMENDED"
        assert reasons[c["contexts"][1]["session_task_id"]] == "SAMPLED"
    with Session(db) as s, s.begin():
        decide(s, c, task=1, action="OVERRIDE", value=3)
        assert settle_session(s, session_id=c["sid"], registry=c["registry"]) is None
    with Session(db) as s, s.begin():
        decide(s, c, operation="second", revision=1, task=0)
        assert settle_session(s, session_id=c["sid"], registry=c["registry"])
    assert c["plugin"].calls == ["task", "task", "paper", "paper", "paper"]
    assert c["adapter"].calls == 2


def test_concurrent_scans_do_not_duplicate(db, two_tasks, tmp_path):
    sid, mid, _, registry = prepare(db, two_tasks, tmp_path, {"version": "v1", "default_ratio": 1})

    def run(_):
        with Session(db) as s, s.begin():
            return settle_session(s, session_id=sid, registry=registry)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, range(2))) == [None, None]
    with Session(db) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(PendingReview)
                .where(PendingReview.manifest_id == mid)
            )
            == 2
        )
        assert s.scalar(select(func.count()).select_from(ReviewDecision)) == 0


def test_v1_zero_automatically_finalizes_and_invalid_waits(db, two_tasks, tmp_path):
    sid, _, _, registry = prepare(db, two_tasks, tmp_path, {"version": "v1", "default_ratio": 0})
    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=sid, registry=registry)
        assert s.get(AssessmentSession, sid).status == "COMPLETED"
    bad_sid, _, _, bad_registry = prepare(
        db, two_tasks, tmp_path, {"version": "v2", "default_ratio": 0}
    )
    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=bad_sid, registry=bad_registry) is None
        assert s.get(AssessmentSession, bad_sid).status == "INPUT_CLOSED"
        assert "Unrecognized" in diagnose_review_policy(
            s, session_id=bad_sid, registry=bad_registry
        )


def test_real_paper_entry_override_selects_only_one(db, two_tasks, tmp_path):
    from app.sessions.assessment_session import SessionTask

    with Session(db) as s:
        selected = s.get(SessionTask, two_tasks[0]["session_task_id"]).paper_entry_id
    policy = {
        "version": "v1",
        "default_ratio": 0,
        "task_type_ratios": {"instruction": 0},
        "paper_entry_ratios": {str(selected): 1},
    }
    sid, mid, _, registry = prepare(db, two_tasks, tmp_path, policy)
    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=sid, registry=registry) is None
        selected_task = s.scalar(
            select(SessionTask).where(
                SessionTask.session_id == sid, SessionTask.paper_entry_id == selected
            )
        )
        pending = list(s.scalars(select(PendingReview).where(PendingReview.manifest_id == mid)))
        assert len(pending) == 1 and pending[0].session_task_id == selected_task.session_task_id
        assert pending[0].reason == "SAMPLED"
