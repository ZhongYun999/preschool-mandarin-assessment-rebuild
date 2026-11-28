"""Offline coverage of iFlytek streaming ISE and durable Repetition execution."""

import base64
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import test_repetition_http as common
import websocket
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evaluation import iflytek_ise as ise
from app.evaluation.models import EvaluationDiagnosticEvidence, EvaluationJob, RawEvaluation
from app.evaluation.worker import claim_job, execute_claim, schedule
from app.media.audio_inspection import AudioTools
from app.media.audio_normalization import AudioSpec
from app.media.recording_store import RecordingStore
from app.scoring.registry import EvaluationContract

pytest_plugins = ("test_repetition_http",)


class FakeSocket:
    def __init__(self, response):
        self.response, self.sent, self.closed = response, [], False

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self):
        if self.response == "provider_error":
            return json.dumps({"code": 10001, "message": "fixture error"})
        if not self.sent or self.sent[-1]["business"].get("aus") != 4:
            raise websocket.WebSocketTimeoutException("no message yet")
        if self.response == "timeout":
            raise TimeoutError("offline timeout")
        return self.response

    def settimeout(self, timeout):
        pass

    def close(self):
        self.closed = True


def test_signature_contains_expected_host_and_time():
    url = ise.signed_url(
        "fixture-key", "fixture-secret", datetime(2020, 12, 22, 6, 29, 31, tzinfo=UTC)
    )
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "wss" and parsed.hostname == ise.HOST and parsed.path == ise.PATH
    assert query["date"] == ["Tue, 22 Dec 2020 06:29:31 GMT"]
    auth = base64.b64decode(query["authorization"][0]).decode()
    assert 'api_key="fixture-key"' in auth and 'headers="host date request-line"' in auth
    assert 'signature="tGKpA/wPww/Xiy0nmTPBOO88BP8MtUUwVplnvslp6X4="' in auth


@pytest.mark.parametrize(
    "scenario", ["ready", "optional", "rejected", "invalid", "timeout", "provider_error"]
)
def test_repetition_to_ise_worker(db, bank, client, repetition, tmp_path, monkeypatch, scenario):
    tools = AudioTools()
    spec = AudioSpec(
        "wav", "pcm_s16le", 16000, 1, "ise-test", tools.version(), tools.version(probe=True)
    )
    params = {
        "category": "read_sentence",
        "group": "adult",
        "grade": None,
        "audio_spec": asdict(spec),
    }
    requirement = {"source": ise.SOURCE, "version": ise.VERSION, "parameters": params}
    base_plugin = common.RepetitionPlugin

    class Plugin(base_plugin):
        def __init__(self, required=False):
            super().__init__(False)
            self.evaluation_requirements["repetition"] = (requirement,)

    contract = EvaluationContract(
        ise.SOURCE, ise.VERSION, frozenset({"repetition"}), ise.valid_parameters, False
    )
    monkeypatch.setattr(common, "RepetitionPlugin", Plugin)
    monkeypatch.setattr(common, "EvaluationContract", lambda *args: contract)
    for name, value in (
        ("IFLYTEK_ISE_APP_ID", "fixture-app-01"),
        ("IFLYTEK_ISE_API_KEY", "fixture-key-01"),
        ("IFLYTEK_ISE_API_SECRET", "fixture-secret-01"),
    ):
        monkeypatch.setenv(name, value)
    flow = repetition(required=True)
    attempt = common.start_ready(client, flow)
    recording = common.start_rec(client, flow, attempt).json()["recording_id"]
    assert common.upload(client, flow, attempt, recording).status_code == 200
    submission = common.submit_http(client, flow, attempt, recording).json()["submission_id"]
    with Session(db) as session, session.begin():
        assert schedule(session) == 1
    xml = '<xml_result><read_sentence><rec_paper><read_sentence total_score="88.5" is_rejected="false" except_info="0" accuracy_score="91"><sentence content="test"><word content="a"><syll perr_msg="1"/></word></sentence></read_sentence></rec_paper></read_sentence></xml_result>'
    if scenario == "rejected":
        xml = xml.replace('is_rejected="false"', 'is_rejected="true"').replace(
            'except_info="0"', 'except_info="28676"'
        )
    if scenario == "optional":
        xml = xml.replace(' accuracy_score="91"', "")
    if scenario == "invalid":
        xml = "<not-xml"
    response = json.dumps(
        {"code": 0, "data": {"status": 2, "data": base64.b64encode(xml.encode()).decode()}}
    )
    ws = FakeSocket(scenario if scenario in ("timeout", "provider_error") else response)
    adapter = ise.IseRepetition(
        flow["store"],
        RecordingStore(tmp_path / "derived"),
        tools,
        websocket_factory=lambda *args, **kwargs: ws,
        sleep=lambda _: None,
    )
    with Session(db) as session:
        job = session.scalar(select(EvaluationJob).where(EvaluationJob.submission_id == submission))
        jid = job.job_id
    work = claim_job(
        db,
        jid,
        registry=flow["registry"],
        adapters={(ise.SOURCE, ise.VERSION): adapter},
        lease=timedelta(minutes=5),
        recording_store=flow["store"],
    )
    assert execute_claim(db, work)
    with Session(db) as session:
        raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == jid))
        expected = {
            "ready": "READY",
            "optional": "READY",
            "rejected": "FAILED",
            "invalid": "INVALID",
            "timeout": "UNKNOWN",
            "provider_error": "FAILED",
        }[scenario]
        assert raw.status == expected
        if expected == "READY":
            assert raw.payload["scores"]["total_score"] == 88.5
            assert raw.payload["scores"]["accuracy_score"] == (
                None if scenario == "optional" else 91
            )
            assert raw.payload["request"]["group"] == "adult"
            assert raw.input_refs["dependencies"]["recording_id"] == recording
        if scenario == "rejected":
            assert raw.payload is None and raw.error_code == "ISE_REJECTED"
            evidence = session.get(EvaluationDiagnosticEvidence, raw.evaluation_id).evidence
            assert (
                evidence["is_rejected"]
                and evidence["except_info"] == "28676"
                and evidence["xml"] == xml
            )
        if scenario == "provider_error":
            assert raw.error_code == "ISE_PROVIDER_ERROR"
            diagnostic = session.get(EvaluationDiagnosticEvidence, raw.evaluation_id).evidence
            assert diagnostic["provider_code"] == 10001
            assert diagnostic["request"]["protocol_version"] == "iflytek-ise-streaming-v2"
    assert ws.closed
    assert ws.sent[0]["business"]["text"] == "\ufefffrozen target"
    assert ws.sent[0]["business"]["category"] == "read_sentence"
    if scenario == "provider_error":
        assert len(ws.sent) == 1
    else:
        assert ws.sent[1]["business"]["aus"] == 1 and ws.sent[-1]["business"]["aus"] == 4


def test_published_sample_and_unsafe_xml():
    from pathlib import Path

    sample = (Path(__file__).parent / "fixtures" / "iflytek_ise_read_sentence.xml").read_text(
        encoding="utf-8"
    )
    result = ise.parse_xml(sample)
    assert result["scores"]["total_score"] == 92.5112
    assert result["scores"]["tone_score"] == 100
    assert any(
        item["level"] == "syll" and item["attributes"].get("dp_message") == "32"
        for item in result["details"]
    )
    with pytest.raises(ValueError, match="Invalid ISE response"):
        ise.parse_xml('<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///secret">]><x>&xxe;</x>')
