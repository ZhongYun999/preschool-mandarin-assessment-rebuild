"""Check UTF-8 Markdown files and relative Markdown file links."""
from pathlib import Path
import os
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def main() -> int:
    errors = []
    paths = []
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "node_modules"}]
        paths.extend(Path(directory) / name for name in files if name.endswith(".md"))
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            errors.append(f"Invalid UTF-8: {path.relative_to(ROOT)}")
            continue
        for link in LINK.findall(content):
            target = urlsplit(link.strip().strip("<>"))
            if target.scheme or target.netloc or not target.path or target.path.startswith("/"):
                continue
            if not (path.parent / unquote(target.path)).exists():
                errors.append(f"Broken link in {path.relative_to(ROOT)}: {link}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Markdown UTF-8 and local file links: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
