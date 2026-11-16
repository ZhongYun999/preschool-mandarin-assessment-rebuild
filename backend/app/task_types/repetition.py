"""Formal submission for a repetition task."""

from app.task_types.simple_recording import submit_recording


def submit_repetition(db, **kwargs):
    return submit_recording(db, task_type="repetition", **kwargs)
