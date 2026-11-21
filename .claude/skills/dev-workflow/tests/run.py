"""Executor da suíte em paralelo, só com a biblioteca padrão.

Uso (da raiz do skill)::

    python3 tests/run.py                       # a suíte inteira, um processo por núcleo
    python3 tests/run.py -p "test_units.py"    # só os arquivos que casam com o padrão
    python3 tests/run.py -k Bootstrap -k Hook  # só os testes cujo nome contém um dos trechos
    python3 tests/run.py -j 1                  # em série, no próprio processo

Os testes são descobertos como no ``python -m unittest discover -s tests`` e distribuídos um a um
entre os processos; cada processo descobre a mesma lista e roda o teste pelo índice, então funções
de teste carregadas por ``load_tests`` também entram. O resultado e o código de saída são os do
``unittest``: 0 sem falha nem erro, 1 caso contrário.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import io
import json
import os
import sys
import time
import traceback
import unittest
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path

TESTS = Path(__file__).resolve().parent
# Duração de cada teste na última execução, para os mais longos começarem primeiro (fora do git).
DURATIONS = TESTS / ".durations.json"
_cache: list[unittest.TestCase] | None = None
_options: tuple[str, str, tuple[str, ...]] = (str(TESTS), "test*.py", ())


def _flatten(suite: unittest.TestSuite | unittest.TestCase) -> Iterator[unittest.TestCase]:
    if isinstance(suite, unittest.TestSuite):
        for item in suite:
            yield from _flatten(item)
    else:
        yield suite


def _selected(test: unittest.TestCase, keywords: tuple[str, ...]) -> bool:
    """O mesmo critério do ``-k`` do ``unittest``: trecho do nome, ou padrão com ``*``."""
    if not keywords:
        return True
    names = (test.id(), str(test))
    return any(
        fnmatchcase(name, keyword if "*" in keyword else f"*{keyword}*") for keyword in keywords for name in names
    )


def discover(start: str, pattern: str, keywords: tuple[str, ...]) -> list[unittest.TestCase]:
    """A lista de testes, sempre na mesma ordem, em qualquer processo."""
    if start not in sys.path:
        sys.path.insert(0, start)
    suite = unittest.TestLoader().discover(start, pattern=pattern, top_level_dir=start)
    tests = [test for test in _flatten(suite) if _selected(test, keywords)]
    broken = [test for test in tests if test.__class__.__name__ == "_FailedTest"]
    return broken + [test for test in tests if test not in broken]


def _initialize(start: str, pattern: str, keywords: tuple[str, ...]) -> None:
    global _cache, _options  # noqa: PLW0603
    _options = (start, pattern, keywords)
    _cache = discover(start, pattern, keywords)


class _Collector(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.outcome = "ok"
        self.detail = ""

    def _set(self, outcome: str, detail: str = "") -> None:
        self.outcome, self.detail = outcome, detail

    def addFailure(self, test, err):
        self._set("fail", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        self._set("error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        self._set("skip", reason)

    def addExpectedFailure(self, test, err):
        self._set("expected", "")

    def addUnexpectedSuccess(self, test):
        self._set("unexpected", "")

    def addSubTest(self, test, subtest, err):
        if err is not None and self.outcome == "ok":
            kind = "fail" if issubclass(err[0], test.failureException) else "error"
            self._set(kind, f"{subtest}\n{self._exc_info_to_string(err, test)}")


def run_one(index: int) -> tuple[int, str, str, str, float]:
    """``(índice, nome, resultado, detalhe, segundos)`` de um teste da lista."""
    if _cache is None:
        _initialize(*_options)
    assert _cache is not None
    test = _cache[index]
    result = _Collector()
    started = time.perf_counter()
    try:
        unittest.TestSuite([test]).run(result)
    except Exception:  # um teste nunca derruba o processo que roda os outros
        result._set("error", traceback.format_exc())
    return index, str(test), result.outcome, result.detail, time.perf_counter() - started


def _load_durations() -> dict[str, float]:
    try:
        data = json.loads(DURATIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(key): float(value) for key, value in data.items()} if isinstance(data, dict) else {}


def _save_durations(measured: dict[str, float]) -> None:
    with contextlib.suppress(OSError):
        DURATIONS.write_text(json.dumps({**_load_durations(), **measured}, indent=0, sort_keys=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests/run.py", description="Roda a suíte do skill em paralelo.")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="processos (padrão: núcleos)")
    parser.add_argument("-s", "--start", default=str(TESTS), help="diretório dos testes (padrão: tests/)")
    parser.add_argument("-p", "--pattern", default="test*.py", help="arquivos de teste (padrão: test*.py)")
    parser.add_argument("-k", dest="keywords", action="append", default=[], help="trecho do nome do teste")
    parser.add_argument("-v", "--verbose", action="store_true", help="uma linha por teste")
    parser.add_argument("--durations", type=int, default=0, metavar="N", help="mostra os N testes mais lentos")
    args = parser.parse_args(argv)
    keywords = tuple(args.keywords)
    start = str(Path(args.start).resolve())
    tests = discover(start, args.pattern, keywords)
    jobs = max(1, min(args.jobs, len(tests) or 1))
    started = time.perf_counter()
    marks = {"ok": ".", "fail": "F", "error": "E", "skip": "s", "expected": "x", "unexpected": "u"}
    outcomes: list[tuple[int, str, str, str, float]] = []

    def report(outcome: tuple[int, str, str, str, float]) -> None:
        outcomes.append(outcome)
        if args.verbose:
            print(f"{outcome[1]} ... {outcome[2]} ({outcome[4]:.1f}s)", flush=True)
        else:
            print(marks[outcome[2]], end="", flush=True)

    if jobs == 1:
        _initialize(start, args.pattern, keywords)
        for index in range(len(tests)):
            report(run_one(index))
    else:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=jobs, initializer=_initialize, initargs=(start, args.pattern, keywords)
        ) as pool:
            # Os mais longos começam primeiro, para o fim da execução não esperar por eles: pela duração
            # da execução anterior e, sem ela, os de ponta a ponta antes dos outros.
            known = _load_durations()
            order = sorted(
                range(len(tests)),
                key=lambda i: -known.get(str(tests[i]), 1000.0 if "e2e" in tests[i].id() else 0.0),
            )
            for outcome in pool.map(run_one, order, chunksize=1):
                report(outcome)
    elapsed = time.perf_counter() - started
    print()
    if start == str(TESTS):
        _save_durations({name: seconds for _, name, _, _, seconds in outcomes})
    outcomes.sort()
    out = io.StringIO()
    for _, name, outcome, detail, _ in outcomes:
        if outcome in ("fail", "error"):
            out.write("=" * 70 + f"\n{'FAIL' if outcome == 'fail' else 'ERROR'}: {name}\n" + "-" * 70 + f"\n{detail}\n")
    print(out.getvalue(), end="")
    if args.durations:
        print(f"Os {args.durations} mais lentos:")
        for _, name, _, _, seconds in sorted(outcomes, key=lambda item: -item[4])[: args.durations]:
            print(f"  {seconds:6.1f}s  {name}")
    count = {key: sum(1 for item in outcomes if item[2] == key) for key in marks}
    print("-" * 70)
    print(f"Ran {len(outcomes)} tests in {elapsed:.3f}s ({jobs} processo(s))\n")
    extra = [
        f"{label}={count[key]}"
        for key, label in (
            ("fail", "failures"),
            ("error", "errors"),
            ("skip", "skipped"),
            ("expected", "expected failures"),
            ("unexpected", "unexpected successes"),
        )
        if count[key]
    ]
    failed = count["fail"] or count["error"] or count["unexpected"]
    print(("FAILED" if failed else "OK") + (f" ({', '.join(extra)})" if extra else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
