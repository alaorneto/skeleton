"""Explicit forms of the platform area; no ModelForm and no mass assignment of identity flags."""

from django import forms

from skeleton.accounts.models import User

__all__ = [
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


class CatalogLabelForm(forms.Form):
    label = forms.CharField(label="Rótulo", max_length=200)
    description = forms.CharField(label="Descrição", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
