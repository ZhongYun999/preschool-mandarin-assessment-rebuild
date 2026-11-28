"""Versioned iFlytek streaming ISE adapter for frozen Repetition submissions."""

import base64
import hashlib
import hmac
import io
import json
import math
import os
import time
import wave
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import urlencode

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from app.evaluation.source_identity import recording_input
from app.evaluation.worker import AdapterResult, InputSnapshot, KnownFailure
from app.media.audio_inspection import AudioError
from app.media.audio_normalization import AudioSpec, prepare_audio

SOURCE = "iflytek-ise"
VERSION = "1"
MODEL = "iflytek-ise-streaming"
HOST = "ise-api.xfyun.cn"
PATH = "/v2/open-ise"
SCORES = (
    "total_score",
    "accuracy_score",
    "fluency_score",
    "integrity_score",
    "phone_score",
    "tone_score",
    "emotion_score",
)


def valid_parameters(params):
    if not isinstance(params, dict) or set(params) != {"category", "group", "grade", "audio_spec"}:
        return False
    if (
        params["category"] != "read_sentence"
        or not isinstance(params["group"], str)
        or params["group"] not in {"adult", "youth", "pupil"}
    ):
        return False
    if params["grade"] not in (None, "junior", "middle", "senior"):
        return False
    if params["grade"] is not None and params["group"] != "pupil":
        return False
    try:
        spec = AudioSpec(**params["audio_spec"])
        return (spec.container, spec.codec, spec.sample_rate, spec.channels) == (
            "wav",
            "pcm_s16le",
            16000,
            1,
        )
    except (TypeError, ValueError):
        return False


def signed_url(api_key, api_secret, now=None):
    date = format_datetime(now or datetime.now(UTC), usegmt=True)
    message = f"host: {HOST}\ndate: {date}\nGET {PATH} HTTP/1.1"
    signature = base64.b64encode(
        hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    authorization = f'api_key="{api_key}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature}"'
    query = urlencode(
        {
            "authorization": base64.b64encode(authorization.encode()).decode(),
            "date": date,
            "host": HOST,
        }
    )
    return f"wss://{HOST}{PATH}?{query}"


def parse_xml(xml):
    try:
        root = ElementTree.fromstring(xml)
        scores = [
            node
            for node in root.iter("read_sentence")
            if "total_score" in node.attrib
            or "is_rejected" in node.attrib
            or "except_info" in node.attrib
        ]
        if len(scores) != 1:
            raise ValueError
        node = scores[0]
        rejected = node.get("is_rejected", "false").lower()
        if rejected not in ("true", "false", "0", "1"):
            raise ValueError
        except_info = node.get("except_info", "0")
        if not except_info.isdigit():
            raise ValueError
        rejected_result = rejected in ("true", "1") or except_info != "0"
        values = {}
        for key in SCORES:
            raw = node.get(key)
            # A rejected result must retain its diagnostic even if its scores are unusable.
            value = None if rejected_result or raw in (None, "") else float(raw)
            if value is not None and not math.isfinite(value):
                raise ValueError
            values[key] = value
        details = [
            {"level": child.tag, "attributes": dict(child.attrib)}
            for child in node.iter()
            if child.tag in ("sentence", "word", "syll", "phone")
        ]
        return {
            "scores": values,
            "is_rejected": rejected in ("true", "1"),
            "except_info": except_info,
            "details": details,
            "xml": xml,
        }
    except (ElementTree.ParseError, DefusedXmlException, ValueError, TypeError, OverflowError):
        raise ValueError("Invalid ISE response") from None


class IseRepetition:
    def __init__(
        self,
        original_store,
        derived_store,
        tools,
        *,
        websocket_factory=None,
        timeout=60,
        sleep=time.sleep,
    ):
        self.original_store, self.derived_store, self.tools = original_store, derived_store, tools
        self.websocket_factory, self.timeout, self.sleep = websocket_factory, timeout, sleep

    def resolve_inputs(self, session, submission, revision, requirement):
        if (
            (requirement.get("source"), requirement.get("version")) != (SOURCE, VERSION)
            or not valid_parameters(requirement.get("parameters"))
            or revision.task_type != "repetition"
            or not isinstance(revision.reference_answer, str)
            or not revision.reference_answer.strip()
        ):
            raise ValueError("Invalid ISE Repetition contract")
        recording = recording_input(session, submission)
        return InputSnapshot(
            refs={
                **{key: recording[key] for key in ("recording_id", "sha256", "media_type")},
                "submission_id": str(submission.submission_id),
                "task_revision_id": str(revision.task_revision_id),
                "contract": {"source": SOURCE, "version": VERSION},
                "parameters": requirement["parameters"],
            },
            payload={"recording": recording, "reference_text": revision.reference_answer},
        )

    def evaluate(self, inputs, requirement):
        import websocket

        params = requirement["parameters"]
        if not valid_parameters(params):
            raise KnownFailure("ISE_INVALID_CONTRACT")
        credentials = tuple(
            os.environ.get(key, "").strip()
            for key in ("IFLYTEK_ISE_APP_ID", "IFLYTEK_ISE_API_KEY", "IFLYTEK_ISE_API_SECRET")
        )
        if not all(credentials):
            raise KnownFailure("ISE_CREDENTIALS_MISSING")
        try:
            prepared = prepare_audio(
                inputs.payload["recording"],
                AudioSpec(**params["audio_spec"]),
                self.original_store,
                self.derived_store,
                self.tools,
            )
            if prepared.output.duration_seconds > 300:
                raise KnownFailure("ISE_AUDIO_INCOMPATIBLE")
            store = self.original_store if prepared.reused_original else self.derived_store
            data = store.read(prepared.storage_ref, prepared.output.sha256)
            with wave.open(io.BytesIO(data), "rb") as wav:
                pcm = wav.readframes(wav.getnframes())
            if not pcm:
                raise KnownFailure("ISE_AUDIO_INCOMPATIBLE")
        except AudioError as exc:
            raise KnownFailure(exc.code) from None
        except (ValueError, OSError, wave.Error, EOFError):
            raise KnownFailure("ISE_AUDIO_INCOMPATIBLE") from None
        app_id, key, secret = credentials
        business = {
            "sub": "ise",
            "cmd": "ssb",
            "ent": "cn_vip",
            "category": params["category"],
            "text": "\ufeff" + inputs.payload["reference_text"],
            "tte": "utf-8",
            "ttp_skip": True,
            "rst": "entirety",
            "rstcd": "utf8",
            "auf": "audio/L16;rate=16000",
            "aue": "raw",
            "group": params["group"],
            "ise_unite": "1",
            "extra_ability": "syll_phone_err_msg;multi_dimension",
        }
        if params["grade"] is not None:
            business["grade"] = params["grade"]
        request_facts = {
            "category": params["category"],
            "group": params["group"],
            "grade": params["grade"],
            "protocol_version": "iflytek-ise-streaming-v2",
        }
        connection = None
        try:
            factory = self.websocket_factory or websocket.create_connection
            connection = factory(signed_url(key, secret), timeout=self.timeout)

            def parse_message(raw):
                message = json.loads(raw)
                if not isinstance(message, dict) or type(message.get("code")) is not int:
                    raise ValueError
                if message["code"]:
                    raise KnownFailure(
                        "ISE_PROVIDER_ERROR",
                        diagnostic={
                            "provider_code": message["code"],
                            "request": request_facts,
                            "audio_evidence": asdict(prepared),
                        },
                    )
                response = message.get("data")
                if not isinstance(response, dict) or response.get("status") not in (0, 1, 2):
                    raise ValueError
                return response

            def poll_early():
                connection.settimeout(0.001)
                try:
                    while True:
                        try:
                            response = parse_message(connection.recv())
                        except websocket.WebSocketTimeoutException:
                            return
                        if response["status"] == 2:
                            raise ValueError("Premature final response")
                finally:
                    connection.settimeout(self.timeout)

            connection.send(
                json.dumps(
                    {"common": {"app_id": app_id}, "business": business, "data": {"status": 0}},
                    ensure_ascii=False,
                )
            )
            poll_early()
            chunks = [pcm[i : i + 1280] for i in range(0, len(pcm), 1280)]
            for index, chunk in enumerate(chunks):
                connection.send(
                    json.dumps(
                        {
                            "business": {"cmd": "auw", "aus": 1 if index == 0 else 2},
                            "data": {"status": 1, "data": base64.b64encode(chunk).decode()},
                        }
                    )
                )
                poll_early()
                if index + 1 < len(chunks):
                    self.sleep(0.04)
            connection.send(
                json.dumps(
                    {"business": {"cmd": "auw", "aus": 4}, "data": {"status": 2, "data": ""}}
                )
            )
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                connection.settimeout(remaining)
                response = parse_message(connection.recv())
                if response["status"] == 2:
                    if (
                        not isinstance(response.get("data"), str)
                        or len(response["data"]) > 2_666_668
                    ):
                        raise ValueError
                    xml = base64.b64decode(response["data"], validate=True).decode("utf-8")
                    if len(xml.encode("utf-8")) > 2_000_000 or any(
                        value in xml for value in credentials
                    ):
                        raise ValueError
                    result = parse_xml(xml)
                    break
        except KnownFailure:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError):
            return AdapterResult(
                payload={"invalid": True}, provider="iflytek", model=MODEL, engine_version=VERSION
            )
        except Exception:  # noqa: BLE001 - dispatch may have reached provider; never expose signed URL
            raise RuntimeError("ISE external outcome unknown") from None
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.close()
        if result["is_rejected"] or result["except_info"] != "0":
            raise KnownFailure(
                "ISE_REJECTED",
                diagnostic={
                    "is_rejected": result["is_rejected"],
                    "except_info": result["except_info"],
                    "details": result["details"],
                    "xml": result["xml"],
                    "request": request_facts,
                    "audio_evidence": asdict(prepared),
                },
            )
        return AdapterResult(
            payload={
                **result,
                "audio_evidence": asdict(prepared),
                "request": request_facts,
            },
            provider="iflytek",
            model=MODEL,
            engine_version=VERSION,
        )

    def validate_result(self, result, requirement):
        payload = result.payload
        if not isinstance(payload, dict) or not valid_parameters(requirement.get("parameters")):
            return False
        scores = payload.get("scores")
        parameters = requirement["parameters"]
        return (
            result.provider == "iflytek"
            and result.model == MODEL
            and result.engine_version == VERSION
            and set(payload)
            == {
                "scores",
                "is_rejected",
                "except_info",
                "details",
                "xml",
                "audio_evidence",
                "request",
            }
            and payload.get("is_rejected") is False
            and payload.get("except_info") == "0"
            and isinstance(scores, dict)
            and set(scores) == set(SCORES)
            and type(scores["total_score"]) in (int, float)
            and all(
                value is None or (type(value) in (int, float) and math.isfinite(value))
                for value in scores.values()
            )
            and isinstance(payload["xml"], str)
            and isinstance(payload["details"], list)
            and isinstance(payload["audio_evidence"], dict)
            and payload["request"]
            == {
                "category": parameters["category"],
                "group": parameters["group"],
                "grade": parameters["grade"],
                "protocol_version": "iflytek-ise-streaming-v2",
            }
        )
