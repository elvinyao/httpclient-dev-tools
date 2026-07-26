from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from resilient_http._vendor import httpx_retries


class VendoredHttpxRetriesTests(unittest.TestCase):
    def test_upstream_046_sources_and_license_are_unmodified(self) -> None:
        expected_sha256 = {
            "__init__.py": "f583559424f3e309cb07124f08cebcd66f487812674acb6aa0dbff397b50c458",
            "retry.py": "f9b1a4acff2eb9e9c2e8921fc7ae0a9becf7b4aab59149a5fdbd03871d0677ed",
            "transport.py": "a832465b0c99f5f020ea495e956a3c10e0a5470fcc9c5d405af6c0b882720ad4",
            "LICENSE": "8c1bcf1aa60b002a93644f4c8804602b0575fa15ea5eeb8a15b1819cac6afbab",
        }
        package_dir = Path(httpx_retries.__file__).parent

        for filename, expected in expected_sha256.items():
            with self.subTest(filename=filename):
                digest = hashlib.sha256((package_dir / filename).read_bytes()).hexdigest()
                self.assertEqual(digest, expected)

    def test_provenance_records_upstream_release(self) -> None:
        package_dir = Path(httpx_retries.__file__).parent
        provenance = (package_dir / "VENDORED.md").read_text(encoding="utf-8")

        self.assertIn("0.4.6", provenance)
        self.assertIn("895e9c1", provenance)
        self.assertIn("Local source changes: none", provenance)


if __name__ == "__main__":
    unittest.main()
