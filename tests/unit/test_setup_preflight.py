"""The setup checks that the instance starts before completing (#467), without a database."""

import logging
import subprocess
import sys

import pytest

from skeleton.setup import preflight


class Result:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def run(monkeypatch):
    calls = []

    def install(result):
        def fake(args, **kwargs):
            calls.append((args, kwargs))
            if isinstance(result, BaseException):
                raise result
            return result

        monkeypatch.setattr(subprocess, "run", fake)
        return calls

    return install


def test_a_clean_start_has_no_problems_and_runs_without_a_shell(run, monkeypatch, tmp_path):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.setup")
    monkeypatch.setenv("SKELETON_SETUP_TARGET", "config.settings.production")
    calls = run(Result(0))
    assert preflight.start_problems("config.settings.production", tmp_path) == []
    [(args, kwargs)] = calls
    assert args[0] == sys.executable and args[1] == "-c" and args[-1] == "config.settings.production"
    assert "shell" not in kwargs and kwargs["timeout"] == 60 and kwargs["cwd"] == str(tmp_path)
    environment = kwargs["env"]
    assert environment["SKELETON_SETUP"] == "off"
    assert "DJANGO_SETTINGS_MODULE" not in environment and "SKELETON_SETUP_TARGET" not in environment


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("django.core.exceptions.ImproperlyConfigured: Produção exige Redis compartilhado.", "Falta o Redis"),
        ("ImproperlyConfigured: Set the CACHE_URL environment variable", "Falta o Redis"),
        ("ImproperlyConfigured: Produção exige SMTP configurado.", "Falta o servidor de e-mail"),
        ("ImproperlyConfigured: Chaves MFA e hosts explícitos são obrigatórios.", "endereços aceitos"),
    ],
)
def test_known_failures_are_translated(run, tmp_path, line, expected):
    run(Result(1, "Traceback (most recent call last):\n  ...\n" + line + "\n"))
    [problem] = preflight.start_problems("config.settings.production", tmp_path)
    assert problem.startswith("A instância não subiria com esta configuração: ") and expected in problem


def test_urls_are_redacted_on_the_page_and_in_the_log(run, tmp_path, caplog):
    secret = "postgres://skeleton_runtime:very-secret@db:5432/app"
    run(Result(1, f"line one {secret}\nValueError: cannot use {secret} " + "x" * 400 + "\n"))
    with caplog.at_level(logging.ERROR, logger="skeleton.setup"):
        [problem] = preflight.start_problems("config.settings.production", tmp_path)
    assert "very-secret" not in problem and "://" not in problem and "[URL]" in problem
    assert len(problem) <= len("A instância não subiria com esta configuração: ") + preflight.LIMIT
    assert "very-secret" not in caplog.text and "[URL]" in caplog.text


def test_timeout_and_unavailable_child(run, tmp_path):
    run(subprocess.TimeoutExpired(["python"], 60))
    assert preflight.start_problems("x", tmp_path) == ["A verificação não terminou em 60 segundos. Tente de novo."]
    run(OSError("no fork"))
    with pytest.raises(preflight.PreflightUnavailable):
        preflight.start_problems("x", tmp_path)
