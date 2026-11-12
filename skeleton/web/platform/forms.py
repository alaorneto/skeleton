"""Explicit forms of the platform area; no ModelForm and no mass assignment of identity flags."""

from django import forms

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
    email = forms.EmailField(label="Email", max_length=254)
    first_name = forms.CharField(label="Nome", max_length=150, required=False)
    last_name = forms.CharField(label="Sobrenome", max_length=150, required=False)
    password1 = forms.CharField(label="Senha", strip=False, widget=forms.PasswordInput(render_value=False))
    password2 = forms.CharField(
        label="Confirmação da senha", strip=False, widget=forms.PasswordInput(render_value=False)
    )

    def clean(self):
        data = super().clean()
        if data.get("password1") and data.get("password1") != data.get("password2"):
            self.add_error("password2", "As senhas não coincidem.")
        return data


class PlatformUserForm(forms.Form):
    first_name = forms.CharField(label="Nome", max_length=150, required=False)
    last_name = forms.CharField(label="Sobrenome", max_length=150, required=False)
    is_active = forms.BooleanField(
        label="Identidade ativa",
        required=False,
        help_text="Desativar revoga as sessões e é recusado se deixar uma organização sem administrador.",
    )
    is_staff = forms.BooleanField(label="Equipe (staff)", required=False)
    is_superuser = forms.BooleanField(
        label="Superusuário",
        required=False,
        help_text="Equipe e superusuário juntos dão acesso à administração da plataforma. "
        "Nenhum dos dois concede acesso aos dados de organizações.",
    )
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


def _owners():
    return User.objects.filter(is_active=True, account_type="human").order_by("email")


class OrganizationCreateForm(forms.Form):
    name = forms.CharField(label="Nome", max_length=200)
    slug = forms.SlugField(label="Identificador (slug)", max_length=100, help_text="Não pode ser alterado depois.")
    owner = forms.ModelChoiceField(
        label="Administrador inicial",
        queryset=User.objects.none(),
        help_text="Recebe o papel de administrador da organização.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["owner"].queryset = _owners()
        self.fields["owner"].label_from_instance = lambda user: user.email


class OrganizationForm(forms.Form):
    name = forms.CharField(label="Nome", max_length=200)
    is_active = forms.BooleanField(
        label="Organização ativa", required=False, help_text="Inativa, ninguém consegue abrir a organização."
    )
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class OrganizationAdministratorForm(forms.Form):
    """Designate an administrator by email; ``replace`` lists only current direct administrators."""

    email = forms.EmailField(
        label="Email da pessoa",
        max_length=254,
        help_text="Identidade ativa que recebe o papel de administrador da organização.",
    )
    replace = forms.TypedChoiceField(
        label="Substituir",
        required=False,
        empty_value=None,
        help_text="Opcional: retira o papel de administrador direto desta pessoa.",
    )
    expected_version = forms.CharField(max_length=64, widget=forms.HiddenInput)

    def __init__(self, *args, administrators=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["replace"].choices = [("", "Ninguém (adicionar)")] + [
            (str(admin.user_id), admin.email) for admin in administrators if admin.direct
        ]

    def clean_email(self):
        from skeleton.accounts.services import normalized_email

        email = normalized_email(self.cleaned_data["email"])
        user = User.objects.filter(email=email, is_active=True, account_type="human").first()
        if user is None:
            raise forms.ValidationError("Não há identidade ativa com este email.")
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
    label = forms.CharField(label="Rótulo", max_length=200)
    description = forms.CharField(label="Descrição", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class StorageQuotaForm(forms.Form):
    """Storage quota of one organization (#61): the default setting, a limit or unlimited."""

    MiB = 1024 * 1024

    mode = forms.ChoiceField(
        label="Cota de armazenamento",
        choices=(("default", "Padrão da instância"), ("limited", "Limite próprio"), ("unlimited", "Ilimitada")),
    )
    quota_mib = forms.IntegerField(
        label="Limite (MiB)", min_value=0, max_value=10**9, required=False, help_text="Só para limite próprio."
    )
    expected_revision = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)

    def clean(self):
        data = super().clean()
        if data.get("mode") == "limited" and data.get("quota_mib") is None:
            self.add_error("quota_mib", "Informe o limite.")
        return data

    def quota_bytes(self):
        data = self.cleaned_data
        return data["quota_mib"] * self.MiB if data["mode"] == "limited" else None
