"""Classify a NUL-delimited git diff; unknown files conservatively run backend checks."""
import sys


def needs_backend(paths: list[str]) -> bool:
    return any(not (p.endswith(".md") and not p.startswith(".github/")) for p in paths)


if __name__ == "__main__":
    paths = [p.decode("utf-8") for p in sys.stdin.buffer.read().split(b"\0") if p]
    print("true" if needs_backend(paths) else "false")
