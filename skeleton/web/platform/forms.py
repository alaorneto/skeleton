"""Explicit forms of the platform area; no ModelForm and no mass assignment of identity flags."""

from django import forms
from django.utils.translation import gettext, gettext_lazy

from skeleton.accounts.models import User

__all__ = [
    "AppAvailabilityForm",
    "AppGrantForm",
    "AppRevokeForm",
    "CatalogLabelForm",
    "OrganizationAdministratorForm",
    "OrganizationCreateForm",
    "OrganizationForm",
    "PlatformUserCreateForm",
    "PlatformUserForm",
]


class PlatformUserCreateForm(forms.Form):
    email = forms.EmailField(label=gettext_lazy("Email"), max_length=254)
    first_name = forms.CharField(label=gettext_lazy("Nome"), max_length=150, required=False)
    last_name = forms.CharField(label=gettext_lazy("Sobrenome"), max_length=150, required=False)
    password1 = forms.CharField(
        label=gettext_lazy("Senha"), strip=False, widget=forms.PasswordInput(render_value=False)
    )
    password2 = forms.CharField(
        label=gettext_lazy("Confirmação da senha"), strip=False, widget=forms.PasswordInput(render_value=False)
    )

    def clean(self):
        data = super().clean()
        if data.get("password1") and data.get("password1") != data.get("password2"):
            self.add_error("password2", gettext("As senhas não coincidem."))
        return data


class RevisionForm(forms.Form):
    """Confirmation pages: carries only the revision the page displayed (#226)."""

    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class PlatformUserForm(forms.Form):
    first_name = forms.CharField(label=gettext_lazy("Nome"), max_length=150, required=False)
    last_name = forms.CharField(label=gettext_lazy("Sobrenome"), max_length=150, required=False)
    is_active = forms.BooleanField(
        label=gettext_lazy("Identidade ativa"),
        required=False,
        help_text=gettext_lazy("Desativar revoga as sessões e é recusado se deixar uma organização sem administrador."),
    )
    is_staff = forms.BooleanField(label="Equipe (staff)", required=False)
    is_superuser = forms.BooleanField(
        label=gettext_lazy("Superusuário"),
        required=False,
        help_text=gettext_lazy(
            "Equipe e superusuário juntos dão acesso à administração da plataforma. "
            "Nenhum dos dois concede acesso aos dados de organizações."
        ),
    )
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


def _owners():
    return User.objects.filter(is_active=True, account_type="human").order_by("email")


class OrganizationCreateForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=200)
    slug = forms.SlugField(
        label="Identificador (slug)", max_length=100, help_text=gettext_lazy("Não pode ser alterado depois.")
    )
    owner = forms.ModelChoiceField(
        label=gettext_lazy("Administrador inicial"),
        queryset=User.objects.none(),
        help_text=gettext_lazy("Recebe o papel de administrador da organização."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["owner"].queryset = _owners()
        self.fields["owner"].label_from_instance = lambda user: user.email


class OrganizationForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=200)
    is_active = forms.BooleanField(
        label=gettext_lazy("Organização ativa"),
        required=False,
        help_text=gettext_lazy("Inativa, ninguém consegue abrir a organização."),
    )
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class OrganizationAdministratorForm(forms.Form):
    """Designate an administrator by email; ``replace`` lists only current direct administrators."""

    email = forms.EmailField(
        label=gettext_lazy("Email da pessoa"),
        max_length=254,
        help_text=gettext_lazy("Identidade ativa que recebe o papel de administrador da organização."),
    )
    replace = forms.TypedChoiceField(
        label=gettext_lazy("Substituir"),
        required=False,
        empty_value=None,
        help_text=gettext_lazy("Opcional: retira o papel de administrador direto desta pessoa."),
    )
    expected_version = forms.CharField(max_length=64, widget=forms.HiddenInput)

    def __init__(self, *args, administrators=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["replace"].choices = [("", gettext("Ninguém (adicionar)"))] + [
            (str(admin.user_id), admin.email) for admin in administrators if admin.direct
        ]

    def clean_email(self):
        from skeleton.accounts.services import normalized_email

        email = normalized_email(self.cleaned_data["email"])
        user = User.objects.filter(email=email, is_active=True, account_type="human").first()
        if user is None:
            raise forms.ValidationError(gettext("Não há identidade ativa com este email."))
        return user

    def clean_replace(self):
        value = self.cleaned_data.get("replace")
        return User.objects.filter(pk=value).first() if value else None


class AppAvailabilityForm(forms.Form):
    """The availability mode of one installed app (#21); ``expected_revision=0`` before the first write."""

    mode = forms.ChoiceField(
        label="Disponibilidade",
        choices=(("all", "Todas as organizações"), ("selected", "Organizações selecionadas")),
        help_text="Disponibilizar não ativa: cada organização ainda escolhe se usa a app.",
    )
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


def _grantable_organizations(granted_ids):
    from skeleton.tenancy.models import Organization

    # The default manager hides soft-deleted organizations; inactive ones cannot be granted either.
    return Organization.objects.filter(is_active=True).exclude(pk__in=granted_ids).order_by("name", "slug")


class AppGrantForm(forms.Form):
    """Grant one more organization a ``selected`` app; the picker offers only active, ungranted ones."""

    organization = forms.ModelChoiceField(label="Organização", queryset=None, empty_label="Escolha…")

    def __init__(self, *args, granted_ids=frozenset(), **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["organization"]
        field.queryset = _grantable_organizations(granted_ids)
        field.label_from_instance = lambda organization: f"{organization.name} ({organization.slug})"
        field.error_messages["invalid_choice"] = "Escolha uma organização ativa que ainda não tenha a app."


class AppRevokeForm(forms.Form):
    organization = forms.UUIDField(widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class CatalogLabelForm(forms.Form):
    label = forms.CharField(label=gettext_lazy("Rótulo"), max_length=200)
    description = forms.CharField(
        label=gettext_lazy("Descrição"), required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class StorageQuotaForm(forms.Form):
    """Storage quota of one organization (#61): the default setting, a limit or unlimited."""

    MiB = 1024 * 1024

    mode = forms.ChoiceField(
        label=gettext_lazy("Cota de armazenamento"),
        choices=(
            ("default", gettext_lazy("Padrão da instância")),
            ("limited", gettext_lazy("Limite próprio")),
            ("unlimited", gettext_lazy("Ilimitada")),
        ),
    )
    quota_mib = forms.IntegerField(
        label="Limite (MiB)",
        min_value=0,
        max_value=10**9,
        required=False,
        help_text=gettext_lazy("Só para limite próprio."),
    )
    expected_revision = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)

    def clean(self):
        data = super().clean()
        if data.get("mode") == "limited" and data.get("quota_mib") is None:
            self.add_error("quota_mib", gettext("Informe o limite."))
        return data

    def quota_bytes(self):
        data = self.cleaned_data
        return data["quota_mib"] * self.MiB if data["mode"] == "limited" else None
