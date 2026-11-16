"""Formal submission for a question-and-answer task."""

from app.task_types.simple_recording import submit_recording


def submit_qa(db, **kwargs):
    return submit_recording(db, task_type="qa", **kwargs)
