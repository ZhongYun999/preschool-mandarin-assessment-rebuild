import unittest

from classify_changes import needs_backend


class ChangeClassificationTest(unittest.TestCase):
    def test_only_markdown_does_not_run_backend(self):
        self.assertFalse(needs_backend(["README.md", "docs/design/contract.md"]))

    def test_code_config_ci_and_unknown_paths_run_backend(self):
        for path in ("backend/app/main.py", "backend/pyproject.toml", "backend/migrations/env.py", ".github/workflows/pr.yml", "future/app.js", ".github/README.md"):
            with self.subTest(path=path):
                self.assertTrue(needs_backend(["docs/a.md", path]))


if __name__ == "__main__":
    unittest.main()
