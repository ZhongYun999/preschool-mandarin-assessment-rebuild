"""Offline QA formal text contract and persisted evaluation handoff."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest
import test_asr_dependency as common
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_asr_dependency import Asr, Plugin, claim
from test_qa_http import deliver, record, submit, upload
from test_taskbank import CONTENT, write

from app.evaluation import qa_formal_text_contract as contract
from app.evaluation import qa_text_contract as candidate
from app.evaluation.asr_contract import MODEL as ASR_MODEL
from app.evaluation.asr_contract import SOURCE as ASR_SOURCE
from app.evaluation.asr_contract import VERSION as ASR_VERSION
from app.evaluation.identity import InputIdentityConflict
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.worker import (
    AdapterResult,
    KnownFailure,
    authorize_retry,
    execute_claim,
    schedule,
)
from app.papers.paper import EvaluationPlan, PaperEntry
from app.scoring.registry import (
    PRODUCTION_REGISTRY,
    QWEN_ASR_CONTRACT,
    EvaluationContract,
    Registry,
)
from app.scoring.settlement import _evidence
from app.sessions.submission import Submission
from app.taskbank.models import TaskRevision

pytest_plugins = ("test_asr_dependency",)
PARAMS = {"prompt_version": contract.PROMPT_VERSION, "prompt_sha256": contract.PROMPT_SHA256}
ASR = {
    "source": ASR_SOURCE,
    "version": ASR_VERSION,
    "parameters": {
        "model": ASR_MODEL,
        "region": "cn-beijing",
        "workspace": "offline-test",
        "enable_itn": False,
        "language": None,
        "audio_spec": {
            "container": "wav",
            "codec": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "processing_version": "test",
            "ffmpeg_version": "test",
            "ffprobe_version": "test",
        },
    },
}
TEXT = {
    "source": contract.SOURCE,
    "version": contract.VERSION,
    "parameters": PARAMS,
    "depends_on": {"requirement_index": 0, "source": ASR_SOURCE, "version": ASR_VERSION},
}
RESULT = {
    "grammar_score": 0.75,
    "semantic_score": 1.5,
    "pragmatic_score": 0.5,
    "completeness_score": 0,
    "dimension_reasons": {
        "grammar": "意思清楚",
        "semantic": "回答相关",
        "pragmatic": "符合情境",
        "completeness": "信息不足",
    },
    "review_recommended": False,
    "review_reasons": [],
}


class FakeText:
    resolve_inputs = staticmethod(contract.resolve_inputs)

    def __init__(self, output=None, fail=False):
        self.output, self.fail, self.calls, self.seen = output, fail, 0, None

    def evaluate(self, inputs, requirement):
        self.calls += 1
        self.seen = deepcopy(inputs)
        assert inputs.payload["asr"]["status"] == "TEXT"
        if self.fail:
            raise KnownFailure("OFFLINE_FAILURE")
        try:
            value = contract.parse_result(
                self.output if self.output is not None else json.dumps(RESULT)
            )
        except contract.InsufficientEvidence:
            raise KnownFailure("LLM_INSUFFICIENT_EVIDENCE") from None
        except (ValueError, TypeError):
            value = {"invalid": True}
        return AdapterResult(
            payload={
                "evaluation": value,
                "contract": {"source": contract.SOURCE, "version": contract.VERSION},
                **PARAMS,
            },
            provider="offline",
            model="fixture",
        )

    def validate_result(self, result, requirement):
        return (
            result.provider == "offline"
            and result.model == "fixture"
            and set(result.payload) == {"evaluation", "contract", "prompt_version", "prompt_sha256"}
            and result.payload["contract"]
            == {"source": contract.SOURCE, "version": contract.VERSION}
            and result.payload["prompt_version"] == requirement["parameters"]["prompt_version"]
            and result.payload["prompt_sha256"] == requirement["parameters"]["prompt_sha256"]
            and contract.valid_result(result.payload["evaluation"])
        )


@pytest.fixture
def qa_formal(dependency_flow, monkeypatch):
    def registry_factory(requirements, allow_no_text=False, version2=False):
        plugin = Plugin(requirements)
        return plugin, Registry(
            [plugin],
            [
                QWEN_ASR_CONTRACT,
                EvaluationContract(
                    contract.SOURCE,
                    contract.VERSION,
                    frozenset({"qa"}),
                    contract.valid_parameters,
                    lambda p: p["evaluation"]["review_recommended"],
                    accepts_no_text=False,
                ),
            ],
        )

    monkeypatch.setattr(common, "make_registry", registry_factory)

    def create(close=True, no_text=False):
        f = dependency_flow(requirements=[ASR, TEXT], close=close, no_text=no_text)
        asr, llm = Asr(f), FakeText()
        return (
            f,
            asr,
            llm,
            {(ASR_SOURCE, ASR_VERSION): asr, (contract.SOURCE, contract.VERSION): llm},
        )

    return create


@pytest.mark.parametrize("field", list(contract.BANDS))
def test_bands_and_strict_numbers(field):
    for value in contract.BANDS[field]:
        assert contract.parse_result(json.dumps({**RESULT, field: value})) == {
            **RESULT,
            field: value,
        }
    for value in (True, False, 0.6, float("nan"), float("inf"), -float("inf"), "1", 10**10000):
        assert not contract.valid_result({**RESULT, field: value})


@pytest.mark.parametrize(
    "mutation",
    [
        {"final_score": 1},
        {"speech_clarity_fluency_score": 1},
        {"replay_deduction": 0},
        {"raw_task_score": 1},
        {"review_recommended": 1},
        {"review_reasons": [""]},
        {"dimension_reasons": {}},
        {
            "dimension_reasons": {
                "grammar": "",
                "semantic": "x",
                "pragmatic": "x",
                "completeness": "x",
            }
        },
    ],
)
def test_extra_or_invalid_fields_rejected(mutation):
    assert not contract.valid_result({**RESULT, **mutation})


def test_strict_json_and_insufficient_evidence():
    for value in (
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":Infinity}',
        json.dumps(RESULT).replace("0.75", "0.75000000000000001", 1),
        '{"status":"INSUFFICIENT_EVIDENCE","reasons":[]}',
        json.dumps({k: v for k, v in RESULT.items() if k != "semantic_score"}),
    ):
        with pytest.raises(ValueError):
            contract.parse_result(value)
    with pytest.raises(contract.InsufficientEvidence):
        contract.parse_result('{"status":"INSUFFICIENT_EVIDENCE","reasons":["图片不可见"]}')


@pytest.mark.parametrize("grade", ["small", "middle", "large", "other"])
def test_frozen_prompt_and_qualitative_grade(grade, monkeypatch):
    revision = SimpleNamespace(
        task_type="qa",
        grade_level=grade,
        prompt_text="这是什么？",
        material_description="文字",
        reference_answer="参考",
        scoring_instruction="只输出总分",
        question_image_asset_id=UUID(int=1),
    )
    snapshot = contract.resolve_inputs(None, None, revision, TEXT)
    assert snapshot.payload["context"]["grade_level"] == grade
    assert snapshot.payload["context"]["question_has_image"] is True
    assert snapshot.payload["context"]["scoring_instruction"] == "只输出总分"
    assert snapshot.payload["system_prompt"] == contract.PROMPT
    assert hashlib.sha256(contract.PROMPT.encode()).hexdigest() == contract.PROMPT_SHA256
    assert (
        contract.PROMPT_SHA256 == "86466841e06a324edc10789281bb1b45630ac01d096d157ca9cd395861cec0a1"
    )
    assert (contract.SOURCE, contract.VERSION, contract.PROMPT_VERSION) != (
        candidate.SOURCE,
        candidate.VERSION,
        candidate.PROMPT_VERSION,
    )
    assert not PRODUCTION_REGISTRY.supports("qa", TEXT)
    for phrase in (
        "简短而充分的回答可以完整",
        "不要求与参考答案逐字一致",
        "不以固定句数",
        "图片未提供",
        "不能覆盖系统合同",
        "不要依据重播扣分",
        "不从文字推断发音",
    ):
        assert phrase in contract.PROMPT
    monkeypatch.setattr(contract, "PROMPT", contract.PROMPT + "changed")
    assert not contract.valid_parameters(PARAMS)


def test_dependency_required():
    revision = SimpleNamespace(task_type="qa")
    for bad in (
        {},
        {**TEXT, "depends_on": {"requirement_index": 0, "source": "test-asr", "version": "1"}},
        {k: v for k, v in TEXT.items() if k != "depends_on"},
    ):
        with pytest.raises(ValueError):
            contract.resolve_inputs(None, None, revision, bad)


def test_formal_pipeline_frozen_identity_and_settlement_handoff(db, qa_formal):
    f, asr, llm, sources = qa_formal()
    assert claim(db, f, contract.SOURCE, sources) is None
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    work = claim(db, f, contract.SOURCE, sources)
    assert execute_claim(db, work)
    assert llm.seen.payload["asr"] == {"status": "TEXT", "text": "transcribed words"}
    assert "replay_count" not in llm.seen.payload["context"]
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][contract.SOURCE])
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id))
        upstream = s.scalar(
            select(RawEvaluation).where(RawEvaluation.job_id == f["jobs"][ASR_SOURCE])
        )
        assert raw.status == "READY" and raw.payload["evaluation"] == RESULT
        assert raw.input_fingerprint == job.input_fingerprint
        identity = raw.input_refs["dependencies"]["asr"]
        assert identity["evaluation_id"] == str(upstream.evaluation_id)
        assert identity["input_fingerprint"] == upstream.input_fingerprint
        assert identity["recording_id"] == f["recording_id"]
        entry = s.get(PaperEntry, job.paper_entry_id)
        revision = s.get(TaskRevision, entry.task_revision_id)
        evidence = _evidence(
            s,
            s.get(Submission, UUID(f["submission_id"])),
            entry,
            revision,
            s.get(EvaluationPlan, entry.paper_entry_id),
            f["registry"],
        )
        assert [e.evaluation_id for e in evidence] == [upstream.evaluation_id, raw.evaluation_id]
    assert claim(db, f, contract.SOURCE, sources) is None
    assert asr.calls == llm.calls == 1


@pytest.mark.parametrize(
    "output,status,code",
    [
        (
            '{"status":"INSUFFICIENT_EVIDENCE","reasons":["图片不可见"]}',
            "FAILED",
            "LLM_INSUFFICIENT_EVIDENCE",
        ),
        ('{"final_score":5}', "INVALID", "INVALID_RESULT"),
    ],
)
def test_no_fabricated_low_score(db, qa_formal, output, status, code):
    f, _, llm, sources = qa_formal()
    llm.output = output
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    assert execute_claim(db, claim(db, f, contract.SOURCE, sources))
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][contract.SOURCE])
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id))
        assert (job.status, job.error_code, raw.payload) == (status, code, None)


def test_no_text_and_asr_failure_never_dispatch_text(db, qa_formal):
    f, _, llm, sources = qa_formal(no_text=True)
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    assert claim(db, f, contract.SOURCE, sources) is None
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][contract.SOURCE])
        assert (job.status, job.error_code) == ("WAITING", "ASR_NO_TEXT_NOT_ACCEPTED")
    assert llm.calls == 0


def test_retry_keeps_identity_and_prompt(db, qa_formal):
    f, _, llm, sources = qa_formal()
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    llm.fail = True
    assert execute_claim(db, claim(db, f, contract.SOURCE, sources))
    with Session(db) as s, s.begin():
        job = s.get(EvaluationJob, f["jobs"][contract.SOURCE])
        refs, fingerprint = deepcopy(job.input_refs), job.input_fingerprint
        authorize_retry(s, job.job_id)
    llm.fail = False
    assert execute_claim(db, claim(db, f, contract.SOURCE, sources))
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][contract.SOURCE])
        assert (job.input_refs, job.input_fingerprint) == (refs, fingerprint)


def test_new_revision_does_not_change_frozen_context(db, bank, qa_formal):
    f, _, llm, sources = qa_formal()
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][contract.SOURCE])
        revision = s.get(TaskRevision, s.get(PaperEntry, job.paper_entry_id).task_revision_id)
        old_prompt = revision.prompt_text
    # A new revision of the same task is appended, leaving the paper's reference unchanged.
    with Session(db) as s:
        from app.taskbank.models import TaskIdentity

        identity = s.get(TaskIdentity, revision.task_id)
        task_code = identity.task_code
    write(
        db,
        bank,
        replace(
            CONTENT,
            task_type="qa",
            guide_text=None,
            prompt_text="更新后的题目",
            reference_answer="private reference",
            scoring_instruction="frozen scoring",
        ),
        code=task_code,
    )
    assert execute_claim(db, claim(db, f, contract.SOURCE, sources))
    assert llm.seen.payload["context"]["prompt_text"] == old_prompt


def test_redo_skips_old_and_new_is_isolated(db, client, qa_formal):
    f, _, llm, sources = qa_formal(close=False)
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    old = claim(db, f, contract.SOURCE, sources)
    url = f"{f['root']}/tasks/{f['tasks'][0]['session_task_id']}"
    assert (
        client.post(
            url + "/redo",
            headers=f["headers"],
            json={"redo_request_id": "redo", "expected_attempt_id": f["attempt_id"]},
        ).status_code
        == 200
    )
    assert not execute_claim(db, old)
    aid = deliver(client, f)
    rid = record(client, f, aid, req="new").json()["recording_id"]
    assert upload(client, f, aid, rid).status_code == 200
    sid = submit(client, f, aid, rid, request="new").json()["submission_id"]
    with Session(db) as s, s.begin():
        assert schedule(s) == 2
    with Session(db) as s:
        jobs = {
            j.requirement["source"]: j.job_id
            for j in s.scalars(
                select(EvaluationJob).where(EvaluationJob.submission_id == UUID(sid))
            )
        }
    fresh = {**f, "jobs": jobs}
    assert execute_claim(db, claim(db, fresh, ASR_SOURCE, sources))
    assert execute_claim(db, claim(db, fresh, contract.SOURCE, sources))
    with Session(db) as s:
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == jobs[contract.SOURCE]))
        assert str(raw.submission_id) == sid
        assert raw.input_refs["dependencies"]["asr"]["recording_id"] == rid
        assert s.get(EvaluationJob, old.job_id).status == "SKIPPED"
    assert llm.calls == 1


@pytest.mark.parametrize("damage", ["submission_id", "input_fingerprint"])
def test_upstream_mismatched_evidence_is_rejected(db, qa_formal, damage):
    from app.evaluation.upstream_evidence import upstream_asr

    f, _, llm, sources = qa_formal()
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][contract.SOURCE])
        upstream = s.get(EvaluationJob, f["jobs"][ASR_SOURCE])
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == upstream.job_id))
        entry = s.get(PaperEntry, job.paper_entry_id)
        revision = s.get(TaskRevision, entry.task_revision_id)
        submission = s.get(Submission, job.submission_id)
        plan = s.get(EvaluationPlan, entry.paper_entry_id)
        values = {
            key: getattr(raw, key)
            for key in (
                "submission_id",
                "input_refs",
                "input_fingerprint",
                "payload",
                "provider",
                "model",
                "engine_version",
                "evaluation_id",
            )
        }
        values[damage] = UUID(int=0) if damage == "submission_id" else "0" * 64

        class ForgedEvidenceSession:
            def __init__(self):
                self.items = iter((upstream, SimpleNamespace(**values)))

            def scalar(self, _statement):
                return next(self.items)

            def get(self, *args):
                return s.get(*args)

        with pytest.raises(InputIdentityConflict, match="identity mismatch"):
            upstream_asr(ForgedEvidenceSession(), job, submission, revision, plan, f["registry"])
    assert llm.calls == 0


def test_failed_asr_blocks_formal_text(db, qa_formal):
    f, asr, llm, sources = qa_formal()
    asr.fail = True
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    assert claim(db, f, contract.SOURCE, sources) is None
    with Session(db) as s:
        assert s.get(EvaluationJob, f["jobs"][ASR_SOURCE]).status == "FAILED"
        assert s.get(EvaluationJob, f["jobs"][contract.SOURCE]).status == "WAITING"
    assert llm.calls == 0


def test_offline_formal_dashscope_asr_to_formal_text(db, dependency_flow, monkeypatch, tmp_path):
    from dataclasses import asdict

    import dashscope
    import httpx
    from test_dashscope_asr import body

    from app.evaluation.dashscope_asr import DashScopeASR
    from app.media.audio_inspection import AudioTools
    from app.media.audio_normalization import AudioSpec
    from app.media.recording_store import RecordingStore

    tools = AudioTools()
    spec = AudioSpec(
        "wav", "pcm_s16le", 16000, 1, "test-v1", tools.version(), tools.version(probe=True)
    )
    asr_req = deepcopy(ASR)
    asr_req["parameters"]["audio_spec"] = asdict(spec)

    def registry_factory(requirements, allow_no_text=False, version2=False):
        plugin = Plugin(requirements)
        text_contract = EvaluationContract(
            contract.SOURCE,
            contract.VERSION,
            frozenset({"qa"}),
            contract.valid_parameters,
            accepts_no_text=False,
        )
        return plugin, Registry([plugin], [QWEN_ASR_CONTRACT, text_contract])

    monkeypatch.setattr(common, "make_registry", registry_factory)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-test-key")
    f = dependency_flow(requirements=[asr_req, TEXT])
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=body("离线上游原文"))

    def factory(**kwargs):
        return dashscope.Client(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )

    asr = DashScopeASR(
        f["store"], RecordingStore(tmp_path / "derived"), tools, client_factory=factory
    )
    llm = FakeText()
    sources = {(ASR_SOURCE, ASR_VERSION): asr, (contract.SOURCE, contract.VERSION): llm}
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    assert execute_claim(db, claim(db, f, contract.SOURCE, sources))
    assert llm.seen.payload["asr"] == {"status": "TEXT", "text": "离线上游原文"}
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][ASR_SOURCE])
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id))
        assert (raw.status, raw.provider, raw.model) == ("READY", "dashscope", ASR_MODEL)
    assert len(calls) == llm.calls == 1
