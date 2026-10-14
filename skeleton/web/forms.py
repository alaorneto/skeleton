from django import forms
from django.db import models

from skeleton.api.serializers import dedicated_model


class ProfileForm(forms.Form):
    first_name = forms.CharField(max_length=150, required=False, label="Nome")
    last_name = forms.CharField(max_length=150, required=False, label="Sobrenome")
    display_name = forms.CharField(max_length=150, required=False, label="Nome de exibição")
    timezone = forms.CharField(max_length=64, label="Fuso horário")
    language = forms.CharField(max_length=16, label="Idioma")
    user_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    profile_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


def _model_form_field(field):
    """Keep Django's constraints without ever constructing a relation select."""
    if field.is_relation:
        return forms.UUIDField(required=not field.blank, label=field.verbose_name,
                               help_text=field.help_text)
    form_field = field.formfield()
    if form_field is None or isinstance(field, models.FileField):
        raise ValueError(f"Unsupported form field: {field.name}")
    for validator in field.validators:
        if validator not in form_field.validators:
            form_field.validators.append(validator)
    return form_field


def explicit_form(model, names, *, user, data=None, instance=None, revision=False):
    fields = {name: _model_form_field(model._meta.get_field(name)) for name in names}
    initial = {}
    if instance is not None:
        from skeleton.web.services import records_data
        projected = records_data([instance], names, user=user)[0]
        initial.update({name: projected.get(name) for name in names})
    if revision:
        fields["revision"] = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
        if instance is not None:
            initial["revision"] = instance.revision
    form_class = type("ExplicitRecordForm", (forms.Form,), fields)
    return form_class(data=data, initial=initial)


def dedicated_form(resource, *, user, data=None, instance=None):
    model, fields = dedicated_model(resource)
    if resource == "memberships" and instance is not None:
        fields = ("is_active",)
    return explicit_form(model, fields, user=user, data=data, instance=instance, revision=instance is not None)


CHANGE_PREFIX = "__change_"


def _optional_change_required(initial):
    # Requiredness is conditional on an opt-in control, which native browser
    # validation cannot express without JS. Django still validates opted-in POSTs.
    return False


class RecordForm(forms.Form):
    def __init__(self, *args, write_only=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.write_only = tuple(write_only)
        self.unchanged = set()
        for name in self.write_only:
            self.fields[name].widget.use_required_attribute = _optional_change_required
            control = CHANGE_PREFIX + name
            self.fields[control] = forms.BooleanField(
                required=False, label=f"Alterar {self.fields[name].label}",
                help_text="Desmarcado mantém o valor atual. Marque para substituir ou limpar.")
            widget = self.fields[control].widget
            changing = self.is_bound and forms.BooleanField(required=False).clean(
                widget.value_from_datadict(self.data, self.files, control))
            if not changing:
                self.unchanged.add(name)
                self.fields[name].required = False
            self.fields[name].initial = None
            self.initial.pop(name, None)

    def _clean_fields(self):
        # Skip validation as well as mutation of unchanged write-only fields.
        # Keep widgets editable for the explicit-change no-JS workflow.
        fields = self.fields
        self.fields = {name: field for name, field in fields.items() if name not in self.unchanged}
        try:
            super()._clean_fields()
        finally:
            self.fields = fields


    def clean(self):
        cleaned = super().clean()
        unknown = set(self.data) - (set(self.fields) | {"csrfmiddlewaretoken"})
        if unknown:
            raise forms.ValidationError("Campo não permitido.")
        for name in self.write_only:
            if not cleaned.get(CHANGE_PREFIX + name):
                cleaned.pop(name, None)
        return cleaned


def record_form(spec, *, user, data=None, instance=None, operation="edit"):
    from skeleton.system.presentation import get_model_ui
    from skeleton.web.services import records_data

    layout = get_model_ui(spec.model._meta.label_lower).form
    declared = {item.name: item for section in layout.sections for item in section.fields}
    fields = {}
    initial = {}
    readable = set(spec.read_fields)
    if instance is not None:
        projected = records_data([instance], tuple(name for name in spec.write_fields if name in readable),
                                 user=user)[0]
        initial.update({name: projected.get(name) for name in spec.write_fields if name in readable})
    for name in spec.write_fields:
        model_field = spec.model._meta.get_field(name)
        field = _model_form_field(model_field)
        item = declared.get(name)
        if item is not None:
            if item.label is not None:
                field.label = item.label
            if item.help_text is not None:
                field.help_text = item.help_text
            widgets = {
                "text": forms.TextInput, "textarea": forms.Textarea,
                "number": forms.NumberInput, "checkbox": forms.CheckboxInput,
                "date": forms.DateInput, "datetime": forms.DateTimeInput, "time": forms.TimeInput,
                "uuid": forms.TextInput, "json": forms.Textarea,
                "reference": forms.TextInput,
            }
            if item.widget == "select":
                field.widget = forms.Select(choices=field.choices)
            elif item.widget is not None:
                field.widget = widgets[item.widget]()
            if item.widget == "date":
                field.widget.input_type = "date"
                field.widget.format = "%Y-%m-%d"
            elif item.widget == "datetime":
                field.widget.input_type = "datetime-local"
                field.widget.format = "%Y-%m-%dT%H:%M:%S"
                field.widget.attrs["step"] = "1"
            elif item.widget == "time":
                field.widget.input_type = "time"
                field.widget.format = "%H:%M:%S"
                field.widget.attrs["step"] = "1"
            if item.rows is not None:
                if not isinstance(field.widget, forms.Textarea):
                    raise forms.ValidationError("rows requires an editable textarea widget.")
                field.widget.attrs["rows"] = item.rows
                field.widget.attrs["data-crud-rows"] = item.rows
        fields[name] = field
    if instance is not None:
        fields["revision"] = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
        initial["revision"] = instance.revision
    write_only = tuple(name for name in spec.write_fields if name not in readable) if instance is not None else ()
    form_class = type("BusinessRecordForm", (RecordForm,), fields)
    return form_class(data=data, initial=initial, write_only=write_only)


def wire_values(cleaned):
    """Omit revision and change controls; unchanged write-only values are absent."""
    return {name: str(value.pk) if hasattr(value, "_meta") else value
            for name, value in cleaned.items()
            if name != "revision" and not name.startswith(CHANGE_PREFIX)}
