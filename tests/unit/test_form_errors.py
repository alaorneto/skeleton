"""Shared ValidationError helpers and regression checks against unguarded ``message_dict`` (#910)."""

from __future__ import annotations

import re
from pathlib import Path

from django import forms
from django.core.exceptions import ValidationError

from skeleton.web.forms import form_error, validation_messages

ROOT = Path(__file__).resolve().parents[2]
MESSAGE_DICT = re.compile(r"\.message_dict\b")
GUARD = re.compile(r"hasattr\([^,]+,\s*['\"](?:error_dict|message_dict)['\"]\)")


class SampleForm(forms.Form):
    name = forms.CharField(required=False)
    days = forms.IntegerField(required=False)


def test_form_error_handles_dict_list_unknown_fields_and_field_map():
    form = SampleForm({})
    assert form.is_valid()
    form_error(form, ValidationError({"name": "Nome inválido.", "unknown": "Outro."}))
    assert form.errors["name"] == ["Nome inválido."]
    assert form.non_field_errors() == ["Outro."]

    mapped = SampleForm({})
    assert mapped.is_valid()
    form_error(mapped, ValidationError({"expires_at": "Prazo longo."}), field_map={"expires_at": "days"})
    assert mapped.errors["days"] == ["Prazo longo."]

    plain = SampleForm({})
    assert plain.is_valid()
    form_error(plain, ValidationError(["Falha geral.", "Segunda falha."]))
    assert plain.non_field_errors() == ["Falha geral.", "Segunda falha."]


def test_validation_messages_flattens_dict_and_list_errors():
    assert validation_messages(ValidationError({"a": ["Um"], "b": ["Dois", "Três"]})) == ["Um", "Dois", "Três"]
    assert validation_messages(ValidationError(["Apenas texto."])) == ["Apenas texto."]


def test_no_unguarded_message_dict_in_skeleton():
    """Every ``.message_dict`` access in ``skeleton/`` is guarded by ``hasattr(..., 'error_dict'|'message_dict')``."""
    offenders: list[str] = []
    for path in sorted((ROOT / "skeleton").rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines, start=1):
            if not MESSAGE_DICT.search(line) or line.lstrip().startswith(("#", '"', "'")):
                continue
            window = "\n".join(lines[max(0, index - 4) : min(len(lines), index + 3)])
            if not GUARD.search(window):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{index}")
    assert offenders == [], "unguarded .message_dict access (use form_error / validation_messages): " + ", ".join(
        offenders
    )
