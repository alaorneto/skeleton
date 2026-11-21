"""O executor paralelo da suíte (``tests/run.py``): mesmo resultado e código de saída do ``unittest``."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUN = Path(__file__).resolve().parent / "run.py"

SAMPLE = """\
import unittest


class SampleTest(unittest.TestCase):
    def test_passes(self):
        self.assertTrue(True)

    def test_fails(self):
        self.assertEqual(1, 2)

    @unittest.expectedFailure
    def test_known_defect(self):
        self.assertEqual(1, 2)

    @unittest.skip("fora do escopo")
    def test_skipped(self):
        pass

    def test_subtest_fails(self):
        for value in (1, 2):
            with self.subTest(value=value):
                self.assertEqual(value, 1)
"""


class RunnerTest(unittest.TestCase):
    def _run(self, source, *args):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "test_sample.py").write_text(source, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(RUN), "-s", directory, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

    def test_a_failing_test_is_reported_and_the_exit_code_is_one(self):
        for jobs in ("1", "3"):
            with self.subTest(jobs=jobs):
                completed = self._run(SAMPLE, "-j", jobs)
                self.assertEqual(completed.returncode, 1, completed.stdout)
                self.assertIn("FAIL: test_fails (test_sample.SampleTest.test_fails)", completed.stdout)
                self.assertIn("AssertionError: 1 != 2", completed.stdout)
                self.assertIn("FAIL: test_subtest_fails", completed.stdout)
                self.assertIn("Ran 5 tests", completed.stdout)
                self.assertIn("FAILED (failures=2, skipped=1, expected failures=1)", completed.stdout)

    def test_a_passing_suite_exits_zero_and_keyword_filters(self):
        completed = self._run(SAMPLE, "-k", "test_passes")
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("Ran 1 tests", completed.stdout)
        self.assertIn("OK", completed.stdout)

    def test_an_import_error_is_an_error(self):
        completed = self._run("import no_such_module_dev_workflow\n")
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertIn("errors=1", completed.stdout)


if __name__ == "__main__":
    unittest.main()
