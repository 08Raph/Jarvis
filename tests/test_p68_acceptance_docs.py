"""docs/ACCEPTANCE_TEST_PLAN.md and section A of docs/WINDOWS_TEST_CHECKLIST.md: every `jarvis ...` command they tell a person to type must be accepted by the
real argument parser (this caught `memory add "text"` with its required category missing), and the plan must keep saying that nothing in it has been run.
This proves the SYNTAX exists. It does not prove any of the acceptance tests passes."""
import re
import shlex
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jarvis import cli  # noqa: E402

PLAN = (ROOT / "docs" / "ACCEPTANCE_TEST_PLAN.md").read_text(encoding="utf-8")
CHECKLIST_A = (ROOT / "docs" / "WINDOWS_TEST_CHECKLIST.md").read_text(encoding="utf-8").split("## Reference lists by prompt")[0]
PLAN_BARE = ("memory ", "desktop ", "voice ", "models ", "selftest", "diagnostics", "privacy", "runtime ")


def accepted(command: str) -> tuple[bool, str]:
    args = [a.strip('"') for a in shlex.split(re.sub(r"<[^>]+>", "X", command), posix=False)]
    out = StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(out):
            cli.build_parser().parse_args(args)
        return True, ""
    except SystemExit as e:
        return e.code == 0, out.getvalue()[-300:]          # --help exits 0


class Commands(unittest.TestCase):
    def test_plan_commands_parse(self):
        found = []
        for m in re.finditer(r"`([^`]+)`", PLAN):
            c = m.group(1).strip()
            if c.startswith("jarvis "):
                found.append(c[len("jarvis "):])
            elif c.startswith(PLAN_BARE) and not c.startswith(("selftest.json", "desktop audit lists")):
                found.append(c)
        self.assertGreaterEqual(len(found), 15)
        for c in found:
            with self.subTest(command=c):
                ok, why = accepted(c)
                self.assertTrue(ok, why)

    def test_checklist_section_a_commands_parse(self):
        found = []
        for line in CHECKLIST_A.splitlines():
            m = re.match(r"\s+jarvis (.*?)(?:\s{2,}\(.*|\s+->.*)?$", line)
            if m:
                found.append(m.group(1).split("   ")[0])
        self.assertGreaterEqual(len(found), 14)
        for c in found:
            with self.subTest(command=c):
                ok, why = accepted(c)
                self.assertTrue(ok, why)


class Honesty(unittest.TestCase):
    def test_the_plan_says_it_is_a_plan_and_names_what_is_not_verified(self):
        self.assertIn("PLAN ONLY", PLAN)
        self.assertIn("NOT RUN anywhere", PLAN)
        for needle in ("AT-05", "Real load + one generation", "never"):
            self.assertIn(needle, PLAN)

    def test_every_test_row_has_an_id_and_failure_criteria(self):
        rows = [r for r in PLAN.splitlines() if r.startswith("| AT-")]
        self.assertGreaterEqual(len(rows), 15)
        for r in rows:
            cells = [c.strip() for c in r.strip("|").split("|")]
            self.assertEqual(len(cells), 6, r[:60])
            self.assertTrue(all(cells), r[:60])


if __name__ == "__main__":
    unittest.main()
