"""Task forms of the flows: date-time picker, person selector and the required mark (#474)."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from django.template.loader import render_to_string
from django.utils import timezone, translation

from skeleton.flows import views
from skeleton.service_api.flows import FormFieldInfo


def info(name, kind, *, required=False, value=None, help_text=""):
    return FormFieldInfo(
        name=name,
        type=kind,
        label=name.capitalize(),
        required=required,
        sensitive=False,
        choices=(),
        help_text=help_text,
        max_length=0,
        value=value,
    )


@pytest.fixture
def members(monkeypatch):
    """OrganizationService.members over a list of people; records how often it is asked."""
    from skeleton.service_api.organizations import OrganizationService

    state = {"people": [], "calls": 0}

    def members(self, *, actor, cursor=None, page_size=25, query=None, group_id=None):
        state["calls"] += 1
        start = int(cursor or 0)
        chunk = state["people"][start : start + page_size]
        following = start + page_size
        return SimpleNamespace(items=chunk, next_cursor=str(following) if following < len(state["people"]) else None)

    monkeypatch.setattr(OrganizationService, "members", members)
    return state


def people(count):
    return [SimpleNamespace(sysid=uuid4(), display_name=f"Pessoa {index}") for index in range(count)]


def test_datetime_is_a_picker_and_reaches_the_service_with_the_offset():
    with translation.override("pt-br"), timezone.override("America/Sao_Paulo"):
        form = views.TaskForm({"due": "2026-12-31T18:00"}, infos=[info("due", "datetime", required=True)])
        assert form.fields["due"].widget.input_type == "datetime-local"
        assert form.is_valid(), form.errors
        assert form.service_data() == (("due", "2026-12-31T18:00:00-03:00"),)
        stored = views.TaskForm(infos=[info("due", "datetime", value="2026-12-31T21:00:00+00:00")])
        assert 'value="2026-12-31T18:00"' in str(stored["due"])
        broken = views.TaskForm(infos=[info("due", "datetime", value="not a date")])
        assert broken.fields["due"].initial is None
        assert not views.TaskForm({"due": "31/12/2026"}, infos=[info("due", "datetime")]).is_valid()


def test_required_mark_comes_from_the_page_not_the_label():
    form = views.TaskForm(infos=[info("summary", "str", required=True), info("note", "str")])
    assert form.fields["summary"].label == "Summary" and not form.fields["summary"].required
    assert form.fields["summary"].widget.attrs["data-required"] == "1"
    with translation.override("pt-br"):
        html = render_to_string("skeleton/web/components/form-fields.html", {"form": form})
    summary = html.split('<label for="id_summary">', 1)[1].split("</label>", 1)[0]
    note = html.split('<label for="id_note">', 1)[1].split("</label>", 1)[0]
    assert '<span class="required" aria-hidden="true"> *</span>' in summary
    assert '<span class="optional">(opcional)</span>' in note
    assert html.count("Campos com * são obrigatórios.") == 1


def test_person_field_is_a_select_of_members_once_per_form(members):
    members["people"] = people(3)
    actor = SimpleNamespace(pk=uuid4())
    form = views.TaskForm(infos=[info("owner", "user"), info("backup", "user")], actor=actor)
    assert members["calls"] == 1  # One lookup for both person fields.
    choices = form.fields["owner"].choices
    assert [label for _, label in choices[1:]] == ["Pessoa 0", "Pessoa 1", "Pessoa 2"]
    chosen = str(members["people"][1].sysid)
    bound = views.TaskForm({"owner": chosen}, infos=[info("owner", "user")], actor=actor)
    assert bound.is_valid() and bound.service_data() == (("owner", chosen),)
    assert UUID(bound.service_data()[0][1])
    foreign = views.TaskForm({"owner": str(uuid4())}, infos=[info("owner", "user")], actor=actor)
    assert not foreign.is_valid()  # A person outside the list is refused before the service.


def test_person_who_left_stays_selectable_as_unavailable(members):
    members["people"] = people(2)
    gone = str(uuid4())
    form = views.TaskForm(infos=[info("owner", "user", value=gone)], actor=SimpleNamespace(pk=uuid4()))
    assert (gone, "Pessoa indisponível") in form.fields["owner"].choices


def test_more_than_200_members_fall_back_to_text(members):
    members["people"] = people(201)
    form = views.TaskForm(infos=[info("owner", "user")], actor=SimpleNamespace(pk=uuid4()))
    assert form.fields["owner"].widget.input_type == "text"
    assert "Administração › Pessoas" in str(form.fields["owner"].help_text)
