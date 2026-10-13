from django import forms

from skeleton.access.policies import authorized_queryset
from skeleton.api.serializers import dedicated_model
from skeleton.common.models import SecuredRecord
from skeleton.web.services import readable_specs


class ProfileForm(forms.Form):
    first_name = forms.CharField(max_length=150, required=False, label="Nome")
    last_name = forms.CharField(max_length=150, required=False, label="Sobrenome")
    display_name = forms.CharField(max_length=150, required=False, label="Nome de exibição")
    timezone = forms.CharField(max_length=64, label="Fuso horário")
    language = forms.CharField(max_length=16, label="Idioma")
    user_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    profile_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


def explicit_form(model, names, *, user, data=None, instance=None, revision=False):
    fields = {}
    initial = {}
    for name in names:
        field = model._meta.get_field(name)
        if field.is_relation:
            related = field.remote_field.model
            if related._meta.label_lower == "sk_accounts.user":
                form_field = forms.UUIDField(required=not field.null, label=field.verbose_name)
            else:
                if issubclass(related, SecuredRecord):
                    queryset = authorized_queryset(related, user=user)
                else:
                    queryset = related.objects.all()
                    if related._meta.label_lower == "sk_system.tabledefinition":
                        keys = [spec.model._meta.label_lower for spec in readable_specs(user)]
                        queryset = queryset.filter(model_label__in=keys, available=True)
                    elif related._meta.label_lower == "sk_tenancy.membership":
                        queryset = queryset.filter(is_active=True)
                form_field = forms.ModelChoiceField(queryset=queryset, required=not field.null, label=field.verbose_name, to_field_name="sysid")
        else:
            form_field = field.formfield()
        fields[name] = form_field
        if instance is not None:
            initial[name] = getattr(instance, field.attname)
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


def wire_values(cleaned):
    """Convert choices to identifiers, reusing the HTTP boundary's reference checks."""
    return {name: str(value.pk) if hasattr(value, "_meta") else value for name, value in cleaned.items() if name != "revision"}
