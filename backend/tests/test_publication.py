import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_paper import draft, entries
from test_taskbank import CONTENT, write

from app.paper import (
    EvaluationPlan,
    Paper,
    PaperEditConflict,
    add_entry,
    copy_paper,
    remove_entry,
    replace_entry,
)
from app.publication import (
    PublicationBlocked,
    preflight_publication,
    publish,
    ready_for_new_session,
    select_plugin,
)
from app.scoring_registry import EvaluationContract, Registry


class TestPlugin:
    __test__ = False
    plugin_id = "test-only"
    implementation_version = "1"
    display_name = "test"
    selectable = True

    def __init__(self):
        self.evaluation_requirements = {
            "qa": ({"source": "fake-llm", "version": "1", "parameters": {"mode": "qa"}},),
            "dialogue": (),
        }

    def score_task(self, task_context, raw_evaluations, submission_facts):
        return None

    def score_paper(self, paper_context, task_scores):
        return None


def registry(plugin=None, contracts=True):
    plugin = plugin or TestPlugin()
    values = (
        [EvaluationContract("fake-llm", "1", frozenset({"qa"}), lambda p: p == {"mode": "qa"})]
        if contracts
        else []
    )
    return Registry([plugin], values)


def setup_paper(db, bank):
    task = write(
        db,
        bank,
        replace(
            CONTENT,
            guide_text="guide",
            prompt_text="full question? next?",
            reference_answer="secret M",
            scoring_instruction="secret N",
            ai_followup_scope="secret O",
        ),
    )
    other = write(
        db, bank, replace(CONTENT, task_type="dialogue", target_rounds=4), code="dialogue"
    )
    paper_id = draft(db, bank)
    with Session(db) as s, s.begin():
        a = add_entry(
            s, paper_id=paper_id, expected_version=0, task_id=task.task_id, position=1
        ).paper_entry_id
        b = add_entry(
            s, paper_id=paper_id, expected_version=1, task_id=other.task_id, position=2
        ).paper_entry_id
    return paper_id, task, other, a, b


def choose(db, paper_id, reg, version=2):
    with Session(db) as s, s.begin():
        select_plugin(
            s,
            paper_id=paper_id,
            expected_version=version,
            plugin_id="test-only",
            version="1",
            registry=reg,
        )


def test_registry_selection_and_missing_plugin_or_contract_block(db, bank):
    paper_id, *_ = setup_paper(db, bank)
    reg = registry()
    assert reg.resolve("test-only", "1") is not None
    assert reg.resolve("test-only", "2") is None
    with pytest.raises(ValueError, match="not selectable"), Session(db) as s, s.begin():
        select_plugin(
            s,
            paper_id=paper_id,
            expected_version=2,
            plugin_id="test-only",
            version="2",
            registry=reg,
        )
    with Session(db) as s:
        assert not preflight_publication(s, paper_id, reg).valid
    choose(db, paper_id, reg)
    with Session(db) as s:
        assert not preflight_publication(s, paper_id, Registry()).valid
        assert not preflight_publication(s, paper_id, registry(contracts=False)).valid
    with pytest.raises(PublicationBlocked), Session(db) as s, s.begin():
        publish(s, paper_id=paper_id, expected_version=3, registry=registry(contracts=False))
    with Session(db) as s:
        assert s.get(Paper, paper_id).status == "DRAFT"
        assert s.scalars(select(EvaluationPlan)).all() == []


def test_preflight_readonly_freeze_and_audio_fields(db, bank):
    paper_id, task, _other, a, b = setup_paper(db, bank)
    reg = registry()
    choose(db, paper_id, reg)
    write(db, bank, replace(CONTENT, prompt_text="new revision"))
    with Session(db) as s:
        result = preflight_publication(s, paper_id, reg)
        assert result.valid and result.edit_version == 3
        assert result.entries[0].revision_id == task.task_revision_id
        assert result.entries[0].fixed_audio == {"guide": "guide", "prompt": "full question? next?"}
        assert result.entries[1].fixed_audio == {"prompt": CONTENT.prompt_text}
        assert result.entries[0].requirements == [
            {"source": "fake-llm", "version": "1", "parameters": {"mode": "qa"}}
        ]
        assert result.entries[1].requirements == []
        assert (
            s.get(Paper, paper_id).status == "DRAFT"
            and s.scalars(select(EvaluationPlan)).all() == []
        )
    with Session(db) as s, s.begin():
        publish(s, paper_id=paper_id, expected_version=3, registry=reg)
    with Session(db) as s:
        assert not ready_for_new_session(s.get(Paper, paper_id))
        assert s.get(EvaluationPlan, a).requirements == result.entries[0].requirements
        assert s.get(EvaluationPlan, b).requirements == []
        assert entries(s, paper_id)[0].task_revision_id == task.task_revision_id
    with pytest.raises(ValueError, match="not a draft"), Session(db) as s, s.begin():
        publish(s, paper_id=paper_id, expected_version=4, registry=reg)


def test_copy_preserves_frozen_requirements_and_explicit_reselection(db, bank):
    paper_id, task, _, a, _ = setup_paper(db, bank)
    original = TestPlugin()
    reg = registry(original)
    choose(db, paper_id, reg)
    with Session(db) as s, s.begin():
        publish(s, paper_id=paper_id, expected_version=3, registry=reg)
    newer = write(db, bank, replace(CONTENT, prompt_text="new"))
    original.evaluation_requirements = {"qa": (), "dialogue": ()}
    assert reg.requirements_for("test-only", "1", "qa")
    changed_reg = registry(original)
    with Session(db) as s, s.begin():
        copy_id = copy_paper(s, source_id=paper_id).paper_id
    with Session(db) as s:
        copied = entries(s, copy_id)
        copy_entry_id = copied[0].paper_entry_id
        assert copied[0].task_revision_id == task.task_revision_id
        assert (
            s.get(EvaluationPlan, copy_entry_id).requirements
            == s.get(EvaluationPlan, a).requirements
        )
        assert not preflight_publication(s, copy_id, Registry()).valid
        assert not preflight_publication(s, copy_id, registry(original, contracts=False)).valid
        assert (
            preflight_publication(s, copy_id, changed_reg).entries[0].requirements[0]["source"]
            == "fake-llm"
        )
    with Session(db) as s, s.begin():
        replace_entry(
            s, paper_id=copy_id, expected_version=0, entry_id=copy_entry_id, task_id=task.task_id
        )
    with Session(db) as s:
        assert entries(s, copy_id)[0].task_revision_id == newer.task_revision_id
        assert s.get(EvaluationPlan, copy_entry_id) is None
        assert preflight_publication(s, copy_id, changed_reg).entries[0].requirements == []


def test_removing_another_papers_entry_does_not_remove_its_plan(db, bank):
    paper_id, *_ = setup_paper(db, bank)
    reg = registry()
    choose(db, paper_id, reg)
    with Session(db) as s, s.begin():
        publish(s, paper_id=paper_id, expected_version=3, registry=reg)
    with Session(db) as s, s.begin():
        first = copy_paper(s, source_id=paper_id).paper_id
        second = copy_paper(s, source_id=paper_id).paper_id
    with Session(db) as s, s.begin():
        other_entry = entries(s, second)[0].paper_entry_id
        with pytest.raises(ValueError, match="does not exist"):
            remove_entry(s, paper_id=first, expected_version=0, entry_id=other_entry)
        assert s.get(EvaluationPlan, other_entry) is not None


def test_database_rejects_missing_plans_and_frozen_mutations(db, bank):
    paper_id, task, _, a, _ = setup_paper(db, bank)
    with pytest.raises(DBAPIError), db.begin() as c:
        c.execute(
            text(
                "UPDATE papers SET status='PUBLISHED', scoring_plugin_id='x', scoring_plugin_version='1' WHERE paper_id=:id"
            ),
            {"id": paper_id},
        )
    reg = registry()
    choose(db, paper_id, reg)
    with pytest.raises(DBAPIError), db.begin() as c:
        c.execute(text("UPDATE papers SET status='PUBLISHED' WHERE paper_id=:id"), {"id": paper_id})
    with Session(db) as s, s.begin():
        publish(s, paper_id=paper_id, expected_version=3, registry=reg)
    for sql in (
        "UPDATE evaluation_plans SET requirements='[]' WHERE paper_entry_id=:id",
        "DELETE FROM evaluation_plans WHERE paper_entry_id=:id",
        "UPDATE paper_entries SET position=42 WHERE paper_entry_id=:id",
    ):
        with pytest.raises(DBAPIError), db.begin() as c:
            c.execute(text(sql), {"id": a})
    with Session(db) as s:
        assert s.get(EvaluationPlan, a).task_revision_id == task.task_revision_id


def test_explicit_plugin_change_revalidates_inherited_requirements(db, bank):
    paper_id, _, _, _, _ = setup_paper(db, bank)
    old, new = TestPlugin(), TestPlugin()
    new.implementation_version = "2"
    new.evaluation_requirements = {"qa": (), "dialogue": ()}
    reg = Registry(
        [old, new],
        [EvaluationContract("fake-llm", "1", frozenset({"qa"}), lambda p: p == {"mode": "qa"})],
    )
    choose(db, paper_id, reg)
    with Session(db) as s, s.begin():
        publish(s, paper_id=paper_id, expected_version=3, registry=reg)
    with Session(db) as s, s.begin():
        copy_id = copy_paper(s, source_id=paper_id).paper_id
    with Session(db) as s, s.begin():
        select_plugin(
            s,
            paper_id=copy_id,
            expected_version=0,
            plugin_id="test-only",
            version="2",
            registry=reg,
        )
    with Session(db) as s:
        result = preflight_publication(s, copy_id, reg)
        assert result.valid and all(entry.requirements == [] for entry in result.entries)
        assert all(s.get(EvaluationPlan, entry.entry_id) is None for entry in result.entries)
    with Session(db) as s, s.begin():
        publish(s, paper_id=copy_id, expected_version=1, registry=reg)


def test_disabled_plugin_cannot_be_newly_selected_but_history_can_be_read(db, bank):
    paper_id, *_ = setup_paper(db, bank)
    plugin = TestPlugin()
    reg = registry(plugin)
    choose(db, paper_id, reg)
    plugin.selectable = False
    with Session(db) as s:
        assert preflight_publication(s, paper_id, reg).valid
    with pytest.raises(ValueError, match="not selectable"), Session(db) as s, s.begin():
        select_plugin(
            s,
            paper_id=paper_id,
            expected_version=3,
            plugin_id="test-only",
            version="1",
            registry=reg,
        )


def test_open_question_ise_string_is_not_a_validated_contract(db, bank):
    paper_id, *_ = setup_paper(db, bank)
    plugin = TestPlugin()
    plugin.evaluation_requirements = {
        "qa": ({"source": "ise", "version": "open-v1", "parameters": {}},),
        "dialogue": (),
    }
    reg = registry(plugin)
    choose(db, paper_id, reg)
    with Session(db) as s:
        result = preflight_publication(s, paper_id, reg)
        assert not result.valid
        assert "Evaluation contract is unavailable or incompatible" in result.entries[0].blockers


def test_concurrent_publish_only_one_succeeds(db, bank):
    paper_id, *_ = setup_paper(db, bank)
    reg = registry()
    choose(db, paper_id, reg)
    barrier = Barrier(3, timeout=10)

    def worker():
        barrier.wait()
        try:
            with Session(db) as s, s.begin():
                publish(s, paper_id=paper_id, expected_version=3, registry=reg)
            return "ok"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        with db.begin() as blocker:
            schema = blocker.scalar(text("SELECT current_schema()"))
            blocker.execute(
                select(Paper.paper_id).where(Paper.paper_id == paper_id).with_for_update()
            )
            futures = [pool.submit(worker) for _ in range(2)]
            barrier.wait()
            deadline = time.monotonic() + 10
            with db.connect() as observer:
                while time.monotonic() < deadline:
                    blocked = observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name=:schema AND cardinality(pg_blocking_pids(pid)) > 0"
                        ),
                        {"schema": schema},
                    )
                    if blocked >= 2:
                        break
                    observer.commit()
                    time.sleep(0.05)
                assert blocked >= 2
        assert sorted(f.result(timeout=10) for f in futures) == ["ok", "rejected"]
    with Session(db) as s:
        assert s.get(Paper, paper_id).status == "PUBLISHED"
        assert len(s.scalars(select(EvaluationPlan)).all()) == 2


def test_edit_and_publish_contend_and_no_partial_freeze(db, bank):
    paper_id, task, _, _, _ = setup_paper(db, bank)
    reg = registry()
    choose(db, paper_id, reg)
    barrier = Barrier(3, timeout=10)

    def worker(kind):
        barrier.wait()
        try:
            with Session(db) as s, s.begin():
                if kind == "publish":
                    publish(s, paper_id=paper_id, expected_version=3, registry=reg)
                else:
                    add_entry(
                        s, paper_id=paper_id, expected_version=3, task_id=task.task_id, position=3
                    )
            return "ok"
        except (PaperEditConflict, ValueError):
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        with db.begin() as c:
            schema = c.scalar(text("SELECT current_schema()"))
            c.execute(select(Paper.paper_id).where(Paper.paper_id == paper_id).with_for_update())
            futures = [pool.submit(worker, kind) for kind in ("publish", "edit")]
            barrier.wait()
            deadline = time.monotonic() + 10
            with db.connect() as observer:
                while time.monotonic() < deadline:
                    blocked = observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name=:schema AND cardinality(pg_blocking_pids(pid)) > 0"
                        ),
                        {"schema": schema},
                    )
                    if blocked >= 2:
                        break
                    observer.commit()
                    time.sleep(0.05)
                assert blocked >= 2
        assert sorted(f.result(timeout=10) for f in futures) == ["ok", "rejected"]
    with Session(db) as s:
        paper = s.get(Paper, paper_id)
        count = len(s.scalars(select(EvaluationPlan)).all())
        assert (paper.status, len(entries(s, paper_id)), count) in [
            ("PUBLISHED", 2, 2),
            ("DRAFT", 3, 0),
        ]
