"""Explicit forms of the platform area; no ModelForm and no mass assignment of identity flags."""

from django import forms
from django.utils.translation import gettext, gettext_lazy

from skeleton.accounts.models import User

__all__ = [
    "AppAvailabilityForm",
    "AppGrantForm",
    "AppLimitsForm",
    "AppRevokeForm",
    "SettingLockForm",
    "SettingUnlockForm",
    "LimitOverrideForm",
    "LimitOverrideRemoveForm",
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


def add_required_rule_fields(form):
    """One field per ``required`` provisioning rule (#703); returns field name → rule target."""
    from skeleton.tenancy.provisioning import required_rules, rule_targets

    targets = rule_targets()
    fields = {}
    for index, rule in enumerate(required_rules()):
        target = targets[rule.key]
        name = f"setting_{index}"
        label = f"{target.group} · {target.label}"
        if target.kind == "bool":
            form.fields[name] = forms.BooleanField(label=label, required=False, help_text=rule.key)
        elif target.kind == "int":
            form.fields[name] = forms.IntegerField(label=label, help_text=rule.key)
        else:
            form.fields[name] = forms.CharField(label=label, max_length=2000, help_text=rule.key)
        fields[name] = target
    return fields


class ProvisioningRuleForm(forms.Form):
    """A rule of the policy of new organizations (#703)."""

    key = forms.ChoiceField(label=gettext_lazy("Configuração"), choices=())
    mode = forms.ChoiceField(
        label=gettext_lazy("Regra"),
        choices=(("default", gettext_lazy("Valor inicial")), ("required", gettext_lazy("Informado na criação"))),
    )
    value = forms.CharField(
        label=gettext_lazy("Valor inicial"),
        required=False,
        max_length=2000,
        help_text=gettext_lazy("Só na regra de valor inicial. Sim/não para ligar e desligar."),
    )
    locked = forms.BooleanField(label=gettext_lazy("Travar na organização"), required=False)
    reason = forms.CharField(label=gettext_lazy("Motivo da trava"), max_length=500, required=False)
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, targets=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["key"].choices = [
            (key, f"{target.group} · {target.label} ({key})") for key, target in (targets or {}).items()
        ]


class ProvisioningRuleRemoveForm(forms.Form):
    key = forms.CharField(max_length=200, widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class OrganizationCreateForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=200)
    slug = forms.SlugField(
        label="Identificador (slug)",
        max_length=63,
        help_text=gettext_lazy("Aparece no endereço da organização; pode ser trocado depois."),
    )
    # An email, not a list of every identity of the installation (#311).
    owner = forms.EmailField(
        label=gettext_lazy("Email do administrador inicial"),
        max_length=254,
        help_text=gettext_lazy("Identidade ativa que recebe o papel de administrador da organização."),
    )

    def __init__(self, *args, **kwargs):
        from django.urls import reverse

        from skeleton.files.models import StorageLocation
        from skeleton.web.slug_check import slug_check_attrs

        super().__init__(*args, **kwargs)
        slug_check_attrs(self.fields["slug"], url=reverse("platform:organization-slug"))
        self.required_rules = add_required_rule_fields(self)
        locations = StorageLocation.objects.filter(is_active=True).order_by("label")
        if locations.exists():
            # Where the organization's files live (#706); it never sees this choice.
            self.fields["storage_location"] = forms.ModelChoiceField(
                label=gettext_lazy("Local de armazenamento"),
                queryset=locations,
                required=False,
                empty_label=gettext("Armazenamento da instância"),
                initial=locations.filter(is_default=True).first(),
            )

    def provided_settings(self):
        """The values of the platform's required rules (#703), by key, typed by the setting kind."""
        from skeleton.tenancy.setting_locks import parse_form_value

        result = {}
        for name, target in self.required_rules.items():
            raw = self.cleaned_data.get(name)
            if target.kind == "bool":
                result[target.key] = bool(raw)
            else:
                result[target.key] = parse_form_value(target, str(raw if raw is not None else ""))
        return result

    def clean_slug(self):
        from skeleton.common.hostnames import slug_problem
        from skeleton.tenancy.services import slug_messages

        value = self.cleaned_data["slug"]
        problem = slug_problem(value)
        if problem is not None:
            raise forms.ValidationError(slug_messages()[problem])
        return value

    def clean_owner(self):
        from skeleton.accounts.services import normalized_email

        user = User.objects.filter(
            email=normalized_email(self.cleaned_data["owner"]), is_active=True, account_type="human"
        ).first()
        if user is None:
            raise forms.ValidationError(gettext_lazy("Não há identidade ativa com este email."))
        return user


class OrganizationForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=200)
    slug = forms.SlugField(
        label="Identificador (slug)",
        max_length=63,
        help_text=gettext_lazy(
            "Aparece no endereço próprio da organização: ao trocá-lo, o endereço antigo deixa de funcionar."
        ),
    )
    is_active = forms.BooleanField(
        label=gettext_lazy("Organização ativa"),
        required=False,
        help_text=gettext_lazy("Inativa, ninguém consegue abrir a organização."),
    )
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def __init__(self, *args, organization=None, **kwargs):
        from django.urls import reverse

        from skeleton.web.slug_check import slug_check_attrs

        super().__init__(*args, **kwargs)
        url = reverse("platform:organization-slug")
        if organization is not None:
            url = f"{url}?organization={organization.pk}"
        slug_check_attrs(self.fields["slug"], url=url, current=organization.slug if organization else "")


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


class AppLimitsForm(forms.Form):
    """What one app adds to each organization limit (#696): one field per registered metric.

    Empty or 0 is no contribution. The field names never carry the metric code itself (an app
    metric has a dot): ``limit_<n>`` in the order of the sorted codes.
    """

    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        from skeleton.common.limits import limit_metric, registered_limit_metrics
        from skeleton.system.limits import MAX_APP_LIMIT

        super().__init__(*args, **kwargs)
        self.metrics: dict[str, str] = {}
        for index, code in enumerate(sorted(registered_limit_metrics())):
            name = f"limit_{index}"
            self.metrics[name] = code
            self.fields[name] = forms.IntegerField(
                label=limit_metric(code).label or code,
                required=False,
                min_value=0,
                max_value=MAX_APP_LIMIT,
                help_text=code,
            )

    @classmethod
    def for_limits(cls, limits, *, revision):
        form = cls(initial={"expected_revision": revision})
        for name, code in form.metrics.items():
            form.initial[name] = limits.get(code) or None
        return form

    def limits(self) -> dict[str, int]:
        return {code: self.cleaned_data[name] for name, code in self.metrics.items() if self.cleaned_data.get(name)}


class LimitOverrideForm(forms.Form):
    """The platform's adjustment of one organization limit (#697): bonus or fixed, with a reason."""

    metric = forms.ChoiceField(label=gettext_lazy("Métrica"), choices=())
    mode = forms.ChoiceField(
        label=gettext_lazy("Ajuste"),
        choices=(("bonus", gettext_lazy("Bônus: soma ao limite calculado")), ("fixed", gettext_lazy("Limite fixo"))),
    )
    value = forms.IntegerField(
        label=gettext_lazy("Valor"),
        required=False,
        min_value=1,
        max_value=2_147_483_647,
        help_text=gettext_lazy("No limite fixo, vazio é ilimitado."),
    )
    reason = forms.CharField(label=gettext_lazy("Motivo"), max_length=500)
    expires_at = forms.DateTimeField(
        label=gettext_lazy("Válido até"),
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"],
        help_text=gettext_lazy("Vazio: sem prazo. Vencido, o ajuste deixa de valer sozinho."),
    )
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        from skeleton.common.limits import limit_metric, registered_limit_metrics

        super().__init__(*args, **kwargs)
        self.fields["metric"].choices = [
            (code, limit_metric(code).label or code) for code in sorted(registered_limit_metrics())
        ]

    def clean(self):
        data = super().clean()
        if data.get("mode") == "bonus" and data.get("value") is None:
            self.add_error("value", gettext("Informe quanto o bônus soma ao limite."))
        return data


class LimitOverrideRemoveForm(forms.Form):
    metric = forms.CharField(max_length=100, widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class SettingLockForm(forms.Form):
    """Lock one setting of one organization (#701): the key, the value typed as text and a reason."""

    key = forms.ChoiceField(label=gettext_lazy("Configuração"), choices=())
    value = forms.CharField(
        label=gettext_lazy("Valor"),
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=gettext_lazy("Sim/não para ligar e desligar; vazio no logo e no ícone (trava o atual)."),
    )
    reason = forms.CharField(label=gettext_lazy("Motivo"), max_length=500)
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, settings=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["key"].choices = [
            (key, f"{setting.group} · {setting.label} ({key})") for key, setting in (settings or {}).items()
        ]


class SettingUnlockForm(forms.Form):
    key = forms.CharField(max_length=200, widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


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
