"""Protect storage roots and the relocated Worker entry point."""

import runpy
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import database
from app.media import dashscope_tts, fixed_audio
from app.taskbank.image_store import ImageStore
from app.taskbank.preview_store import PreviewStore


def test_default_storage_roots_remain_at_repository_root(monkeypatch):
    for key in ("TASKBANK_ASSET_ROOT", "TASKBANK_PREVIEW_ROOT", "FIXED_AUDIO_ROOT"):
        monkeypatch.delenv(key, raising=False)
    uploads = Path(__file__).resolve().parents[2] / "uploads"
    assert ImageStore().root == uploads / "taskbank"
    assert PreviewStore().root == uploads / "import-previews"
    assert fixed_audio.AudioStore().root == uploads / "fixed-audio"


def test_relocated_worker_entry_starts_without_external_calls(monkeypatch):
    class StopWorker(Exception):
        pass

    class FakeEngine:
        disposed = False

        def dispose(self):
            self.disposed = True

    class FakeSession:
        def __init__(self, engine):
            assert engine is fake_engine

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def scalars(self, query):
            scans.append(query)
            return SimpleNamespace(all=list)

    class FakeSynthesizer:
        def synthesize(self, *args):
            raise AssertionError("unexpected external call")

    def stop(_seconds):
        raise StopWorker

    fake_engine, scans = FakeEngine(), []
    monkeypatch.setattr(database, "create_database_engine", lambda: fake_engine)
    monkeypatch.setattr(dashscope_tts, "DashScopeSynthesizer", FakeSynthesizer)
    monkeypatch.setattr("sqlalchemy.orm.Session", FakeSession)
    monkeypatch.setattr(time, "sleep", stop)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=".*found in sys.modules.*", category=RuntimeWarning
        )
        with pytest.raises(StopWorker):
            runpy.run_module("app.media.tts_worker", run_name="__main__")
    assert len(scans) == 1
    assert fake_engine.disposed
