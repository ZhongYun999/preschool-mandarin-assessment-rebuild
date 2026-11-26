"""Speaking text evaluation exercises the worker with only offline substitutes."""

import json
import math
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_speaking_evaluation_input import claim, make_case
from test_speaking_formal_asr import ASR, CONTRACTS, MultiASR
from test_speaking_process_asr import REQUIREMENT, FakeASR, segment

from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.speaking_text_contract import (
    BANDS,
    PROMPT,
    PROMPT_SHA256,
    PROMPT_VERSION,
    SOURCE,
    VERSION,
    InsufficientEvidence,
    parse_result,
    resolve_inputs,
    valid_consumption,
    valid_parameters,
    valid_result,
)
from app.evaluation.worker import (
    AdapterResult,
    InputIdentityConflict,
    KnownFailure,
    authorize_retry,
    execute_claim,
    schedule,
)
from app.scoring.registry import PRODUCTION_REGISTRY, EvaluationContract
from app.task_types.speaking_asr_worker import claim as claim_process_asr
from app.task_types.speaking_asr_worker import execute as execute_process_asr

pytest_plugins = ("test_speaking_process_asr",)
PARAMS = {"prompt_version": PROMPT_VERSION, "prompt_sha256": PROMPT_SHA256}
TEXT = {
    "source": SOURCE,
    "version": VERSION,
    "parameters": PARAMS,
    "depends_on": {"requirement_index": 0, "source": ASR["source"], "version": ASR["version"]},
}
CONTRACT = EvaluationContract(
    SOURCE,
    VERSION,
    frozenset({"speaking"}),
    valid_parameters,
    accepts_no_text=True,
    speaking_consumption_validator=valid_consumption,
)
RESULT = {
    "content_coherence_score": 1.5,
    "vocabulary_grammar_score": 0.75,
    "expression_naturalness_score": 0.75,
    "dimension_reasons": {
        "content_coherence": "围绕主题。",
        "vocabulary_grammar": "意思清楚。",
        "expression_naturalness": "措辞自然。",
    },
    "review_recommended": False,
    "review_reasons": [],
}


class FakeTextAdapter:
    resolve_inputs = staticmethod(resolve_inputs)

    def __init__(self, output=None, fail=False):
        self.output, self.fail, self.calls, self.seen = output, fail, 0, None

    def evaluate(self, inputs, requirement):
        self.calls += 1
        self.seen = deepcopy(inputs)
        if self.fail:
            raise KnownFailure("OFFLINE_FAILURE")
        if all(s["status"] == "NO_TEXT" for s in inputs.payload["asr"]["segments"]):
            raise KnownFailure("LLM_INSUFFICIENT_EVIDENCE")
        try:
            value = parse_result(self.output if self.output is not None else json.dumps(RESULT))
        except InsufficientEvidence:
            raise KnownFailure("LLM_INSUFFICIENT_EVIDENCE") from None
        except (TypeError, ValueError):
            value = {"invalid": True}
        return AdapterResult(
            payload={
                "evaluation": value,
                "prompt_version": PROMPT_VERSION,
                "prompt_sha256": PROMPT_SHA256,
                "contract": {"source": SOURCE, "version": VERSION},
            },
            provider="offline",
            model="fixture-text",
        )

    def validate_result(self, result, requirement):
        return (
            result.provider == "offline"
            and result.model == "fixture-text"
            and set(result.payload) == {"evaluation", "prompt_version", "prompt_sha256", "contract"}
            and valid_result(result.payload["evaluation"])
            and result.payload["prompt_version"] == requirement["parameters"]["prompt_version"]
            and result.payload["prompt_sha256"] == requirement["parameters"]["prompt_sha256"]
            and result.payload["contract"] == {"source": SOURCE, "version": VERSION}
        )


@pytest.mark.parametrize("field", list(BANDS))
def test_exact_allowed_bands(field):
    for value in BANDS[field]:
        assert valid_result({**RESULT, field: value})
    for value in (True, False, 0.6, float("nan"), float("inf"), -math.inf, "1"):
        assert not valid_result({**RESULT, field: value})


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"final_score": 4},
        {"help_deduction": 0.25},
        {"speech_score": 1},
        {"review_recommended": "false"},
        {"review_reasons": [""]},
        {"dimension_reasons": {}},
        {"dimension_reasons": {"content_coherence": ""}},
    ],
)
def test_bad_result_fields_are_rejected(change):
    assert not valid_result({**RESULT, **change})


def test_json_and_insufficient_evidence_are_distinct():
    assert parse_result(json.dumps(RESULT)) == RESULT
    with pytest.raises(ValueError, match="Duplicate"):
        parse_result('{"x": 1, "x": 2}')
    with pytest.raises(ValueError):
        parse_result('{"content_coherence_score": NaN}')
    almost = json.dumps(RESULT).replace("0.75", "0.75000000000000001", 1)
    with pytest.raises(ValueError):
        parse_result(almost)
    assert not valid_result({**RESULT, "content_coherence_score": 10**10000})
    with pytest.raises(InsufficientEvidence):
        parse_result('{"status":"INSUFFICIENT_EVIDENCE","reasons":["上下文不足"]}')
    with pytest.raises(ValueError):
        parse_result('{"status":"INSUFFICIENT_EVIDENCE","reasons":[]}')
    assert not valid_result({k: v for k, v in RESULT.items() if k != "content_coherence_score"})


@pytest.mark.parametrize("grade", ["small", "middle", "large"])
def test_grade_and_prompt_are_frozen_without_sentence_gates(grade):
    revision = SimpleNamespace(
        grade_level=grade,
        prompt_text="讲述",
        material_description=None,
        reference_answer=None,
        scoring_instruction=None,
        question_image_asset_id=None,
    )
    submission = SimpleNamespace(inputs={"segments": [{"segment_id": "a"}]})
    snapshot = resolve_inputs(None, submission, revision, TEXT)
    assert snapshot.payload["context"]["grade_level"] == grade
    assert snapshot.payload["system_prompt"] == PROMPT
    assert "至少五个完整中文句子" not in PROMPT and "至少5个完整" not in PROMPT
    assert "不以固定句数" in PROMPT and valid_parameters(PARAMS)
    assert not valid_parameters({**PARAMS, "prompt_sha256": "0" * 64})
    assert not PRODUCTION_REGISTRY.supports("speaking", TEXT)


def test_changed_prompt_cannot_reuse_frozen_identity(monkeypatch):
    import app.evaluation.speaking_text_contract as contract

    monkeypatch.setattr(contract, "PROMPT", contract.PROMPT + "changed")
    assert not contract.valid_parameters(PARAMS)


def setup_case(db, client, flow, monkeypatch, count):
    return make_case(db, client, flow, monkeypatch, count, [ASR, TEXT], [CONTRACTS[0], CONTRACT])


@pytest.mark.parametrize("count", [1, 2, 3])
def test_complete_offline_asr_to_text_raw(db, client, flow, monkeypatch, count):
    _, _, _, _, sid, registry, store, ids = setup_case(db, client, flow, monkeypatch, count)
    text = FakeTextAdapter()
    assert claim(db, ids[1], registry, store, text, SOURCE) is None and text.calls == 0
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    work = claim(db, ids[1], registry, store, text, SOURCE)
    assert work and execute_claim(db, work)
    assert [s["text"] for s in text.seen.payload["asr"]["segments"]] == [
        f"fixture-{i}" for i in range(count)
    ]
    assert set(text.seen.payload) == {"context", "system_prompt", "asr"}
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[1])
        raw = session.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == ids[1], RawEvaluation.status == "READY"
            )
        )
        asr = session.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == ids[0], RawEvaluation.status == "READY"
            )
        )
        assert (
            raw.submission_id == sid and raw.provider == "offline" and raw.model == "fixture-text"
        )
        assert raw.input_fingerprint == job.input_fingerprint
        assert raw.input_refs["dependencies"]["asr"]["evaluation_id"] == str(asr.evaluation_id)
        assert raw.input_refs["dependencies"]["asr"]["input_fingerprint"] == asr.input_fingerprint
        assert raw.payload["evaluation"] == RESULT and raw.payload["prompt_sha256"] == PROMPT_SHA256
        from app.papers.paper import EvaluationPlan, PaperEntry
        from app.scoring.settlement import _evidence
        from app.sessions.submission import Submission
        from app.taskbank.models import TaskRevision

        entry = session.get(PaperEntry, job.paper_entry_id)
        revision = session.get(TaskRevision, entry.task_revision_id)
        evidence = _evidence(
            session,
            session.get(Submission, sid),
            entry,
            revision,
            session.get(EvaluationPlan, entry.paper_entry_id),
            registry,
        )
        assert [item.evaluation_id for item in evidence] == [asr.evaluation_id, raw.evaluation_id]
    assert claim(db, ids[1], registry, store, text, SOURCE) is None
    assert text.calls == 1


@pytest.mark.parametrize(
    "output,error",
    [
        (
            '{"status":"INSUFFICIENT_EVIDENCE","reasons":["缺少图片上下文"]}',
            "LLM_INSUFFICIENT_EVIDENCE",
        ),
        ('{"final_score":5}', "INVALID_RESULT"),
    ],
)
def test_insufficient_and_invalid_never_create_ready(db, client, flow, monkeypatch, output, error):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    assert execute_claim(db, claim(db, ids[1], registry, store, FakeTextAdapter(output), SOURCE))
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[1])
        assert job.status == ("FAILED" if error == "LLM_INSUFFICIENT_EVIDENCE" else "INVALID")
        assert job.error_code == error
        assert (
            session.scalar(
                select(RawEvaluation).where(
                    RawEvaluation.job_id == ids[1], RawEvaluation.status == "READY"
                )
            )
            is None
        )


def test_retry_preserves_input_identity(db, client, flow, monkeypatch):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, 2)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    assert execute_claim(db, claim(db, ids[1], registry, store, FakeTextAdapter(fail=True), SOURCE))
    with Session(db) as session, session.begin():
        job = session.get(EvaluationJob, ids[1])
        refs, fingerprint = deepcopy(job.input_refs), job.input_fingerprint
        authorize_retry(session, job.job_id)
    assert execute_claim(db, claim(db, ids[1], registry, store, FakeTextAdapter(), SOURCE))
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[1])
        assert job.input_refs == refs and job.input_fingerprint == fingerprint


def test_mixed_no_text_is_preserved_and_insufficient_does_not_score(db, client, flow, monkeypatch):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, 2)

    def mixed(payload):
        payload["segments"][1].update(status="NO_TEXT", text=None)

    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(mutation=mixed, store=store), ASR["source"])
    )
    adapter = FakeTextAdapter('{"status":"INSUFFICIENT_EVIDENCE","reasons":["部分上下文不足"]}')
    assert execute_claim(db, claim(db, ids[1], registry, store, adapter, SOURCE))
    assert [s["status"] for s in adapter.seen.payload["asr"]["segments"]] == ["TEXT", "NO_TEXT"]
    assert adapter.seen.payload["asr"]["segments"][1]["text"] is None
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[1]).error_code == "LLM_INSUFFICIENT_EVIDENCE"
        assert (
            session.scalar(
                select(RawEvaluation).where(
                    RawEvaluation.job_id == ids[1], RawEvaluation.status == "READY"
                )
            )
            is None
        )


def test_all_no_text_does_not_produce_score_and_is_not_qwen_capability(
    db, client, flow, monkeypatch
):
    from app.evaluation.speaking_qwen_contract import valid_parameters as qwen_parameters

    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(no_text=True, store=store), ASR["source"])
    )
    adapter = FakeTextAdapter()
    assert execute_claim(db, claim(db, ids[1], registry, store, adapter, SOURCE))
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[1]).error_code == "LLM_INSUFFICIENT_EVIDENCE"
    valid_qwen = {
        "provider": "dashscope",
        "model": "qwen3-asr-flash",
        "region": "cn-beijing",
        "workspace": "test-workspace",
        "enable_itn": False,
        "language": "zh",
        "processing_version": "1",
        "confirmed_no_text": False,
        "audio_spec": {
            "container": "wav",
            "codec": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "processing_version": "1",
            "ffmpeg_version": "fixture",
            "ffprobe_version": "fixture",
        },
    }
    assert qwen_parameters(valid_qwen)
    assert not qwen_parameters({**valid_qwen, "confirmed_no_text": True})


def test_subset_upstream_cannot_be_scored_as_full_submission(db, client, flow, monkeypatch):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, 3)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(subset=True, store=store), ASR["source"])
    )
    adapter = FakeTextAdapter()
    with pytest.raises(InputIdentityConflict):
        claim(db, ids[1], registry, store, adapter, SOURCE)
    assert adapter.calls == 0


@pytest.mark.parametrize("damage", ["reverse", "duplicate", "missing", "foreign", "fingerprint"])
def test_tampered_snapshot_never_reaches_model(db, client, flow, monkeypatch, damage):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, 2)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    adapter = FakeTextAdapter()
    work = claim(db, ids[1], registry, store, adapter, SOURCE)
    segments = work.inputs.payload["asr"]["segments"]
    if damage == "reverse":
        segments.reverse()
    elif damage == "duplicate":
        segments[1] = deepcopy(segments[0])
    elif damage == "missing":
        segments.pop()
    elif damage == "foreign":
        segments[0]["recording_id"] = "different-submission"
    else:
        work.inputs.refs["asr"]["input_fingerprint"] = "0" * 64
    try:
        assert not execute_claim(db, work)
    except ValueError as exc:
        assert "frozen input identity" in str(exc)
    assert adapter.calls == 0


@pytest.mark.parametrize("transcript", ["猫", "我去了公园然后看到小猫", "我，去，了，公，园。"])
def test_no_punctuation_or_length_gate(db, client, flow, monkeypatch, transcript):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, 1)

    def replace_text(payload):
        payload["segments"][0]["text"] = transcript

    assert execute_claim(
        db,
        claim(
            db, ids[0], registry, store, MultiASR(mutation=replace_text, store=store), ASR["source"]
        ),
    )
    adapter = FakeTextAdapter()
    assert execute_claim(db, claim(db, ids[1], registry, store, adapter, SOURCE))
    assert adapter.seen.payload["asr"]["segments"][0]["text"] == transcript


def test_input_closed_still_allows_frozen_evaluation(db, client, flow, monkeypatch):
    root, _, headers, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, 1)
    response = client.post(root + "/close", headers=headers, json={"close_request_id": "close"})
    assert response.status_code == 200
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    assert execute_claim(db, claim(db, ids[1], registry, store, FakeTextAdapter(), SOURCE))
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[1]).status == "READY"


def test_redo_cannot_reuse_old_text_result(db, client, flow, monkeypatch):
    root, base, headers, old_attempt, old_submission, registry, store, ids = setup_case(
        db, client, flow, monkeypatch, 1
    )
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    assert execute_claim(db, claim(db, ids[1], registry, store, FakeTextAdapter(), SOURCE))
    redo = client.post(
        base + "/redo",
        headers=headers,
        json={"redo_request_id": "redo", "expected_attempt_id": old_attempt},
    )
    assert redo.status_code == 200
    attempt = redo.json()["attempt_id"]
    playback = client.post(
        base + "/playbacks",
        headers=headers,
        json={"attempt_id": attempt, "request_id": "prompt", "kind": "INITIAL_PROMPT"},
    )
    assert playback.status_code == 200
    assert (
        client.post(
            base + "/playbacks/" + playback.json()["playback_id"] + "/finish",
            headers=headers,
            json={"attempt_id": attempt, "completed": True},
        ).status_code
        == 200
    )
    operation, _ = segment(client, root, base, headers, attempt, "new-record")
    assert execute_process_asr(db, claim_process_asr(db, operation, REQUIREMENT), FakeASR())
    response = client.post(
        base + "/submissions/speaking",
        headers=headers,
        json={"attempt_id": attempt, "finish_request_id": "finish-new"},
    )
    assert response.status_code == 200
    new_submission = UUID(response.json()["submission_id"])
    with Session(db) as session, session.begin():
        assert schedule(session) == 2
        jobs = list(
            session.scalars(
                select(EvaluationJob)
                .where(EvaluationJob.submission_id == new_submission)
                .order_by(EvaluationJob.requirement_index)
            )
        )
        new_asr, new_text = [job.job_id for job in jobs]
        old_raw = session.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == ids[1], RawEvaluation.status == "READY"
            )
        )
        old_raw_id = old_raw.evaluation_id
        assert old_raw.submission_id == old_submission
    assert claim(db, new_text, registry, store, FakeTextAdapter(), SOURCE) is None
    assert execute_claim(
        db, claim(db, new_asr, registry, store, MultiASR(store=store), ASR["source"])
    )
    assert execute_claim(db, claim(db, new_text, registry, store, FakeTextAdapter(), SOURCE))
    with Session(db) as session:
        new_raw = session.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == new_text, RawEvaluation.status == "READY"
            )
        )
        assert new_raw.submission_id == new_submission and new_raw.evaluation_id != old_raw_id
