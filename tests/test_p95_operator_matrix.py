"""The capability matrix must match the registry exactly, and the shipped doc must be the generated one."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.operator import core, matrix  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Matrix(unittest.TestCase):
    def setUp(self):
        core.load_all()

    def test_every_pattern_matches_something(self):
        for area, _c, pats, _n in matrix.ROWS:
            for p in pats:
                self.assertTrue(any(matrix._matches(p, k) for k in core.REGISTRY), f"{area}: {p}")

    def test_every_capability_is_listed_in_the_matrix(self):
        self.assertEqual([k for k, v in matrix.covered(core.REGISTRY).items() if not v], [])

    def test_classes_are_valid_and_the_unimplemented_are_honest(self):
        for area, c, pats, note in matrix.ROWS:
            self.assertIn(c, matrix.CLASSES)
            self.assertTrue(note.strip(), area)
            if c in (4, 6, 7):
                self.assertEqual(pats, (), f"{area} is marked not available but lists capabilities")

    def test_no_class_1_row_claims_windows_only_capabilities(self):
        for area, c, pats, _n in matrix.ROWS:
            if c == 1:
                for k, cp in core.REGISTRY.items():
                    if any(matrix._matches(p, k) for p in pats) and cp.windows_only:
                        self.fail(f"{area}: {k} is Windows-only but the row says class 1")

    def test_shipped_doc_is_the_generated_one(self):
        doc = (ROOT / "docs" / "CAPABILITY_MATRIX.md").read_text(encoding="utf-8")
        self.assertEqual(doc.strip(), matrix.render(core.REGISTRY).strip(), "regenerate: python -m jarvis.operator.matrix > docs/CAPABILITY_MATRIX.md")


if __name__ == "__main__":
    unittest.main()
