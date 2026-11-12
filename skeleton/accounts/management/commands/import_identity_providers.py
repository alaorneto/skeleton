"""Import the corporate OIDC/SAML providers of the private JSON file into the database (#29)."""

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from skeleton.tenancy.context import system_context

REASON = "idp:import"


class _DryRun(Exception):
    pass


def _row_data(binding, organization):
    app = binding.app
    options = app.get("settings", {})
    data = {
        "provider_id": binding.provider_id,
        "protocol": binding.protocol,
        "bound_organization": organization,
        "client_id": app["client_id"],
        "name": app.get("name") or binding.provider_id,
        "trust_verified_email": binding.trust_verified_email,
    }
    if binding.protocol == "oidc":
        data["issuer"] = options["server_url"].rstrip("/")
        data["secret"] = app.get("secret") or ""
    else:
        idp = options.get("idp", {})
        sp = options.get("sp", {})
        advanced = options.get("advanced", {})
        data["entity_id"] = idp["entity_id"]
        data["sp_entity_id"] = sp.get("entity_id", "")
        data["settings"] = {
            "idp": {key: idp[key] for key in ("sso_url", "slo_url", "metadata_url", "x509cert") if idp.get(key)},
            "sp": {"x509cert": advanced["x509cert"]} if advanced.get("x509cert") else {},
            "attribute_mapping": dict(options.get("attribute_mapping", {})),
        }
        private_key = advanced.get("private_key") or sp.get("private_key")
        if private_key:
            data["private_key"] = private_key
    return data


class Command(BaseCommand):
    help = (
        "Import the oidc/saml providers of SSO_PROVIDER_CONFIG_PATH (or --from-config) into the database, "
        "with the same binding fingerprint, so existing SocialAccounts keep working. gov.br and GitHub "
        "stay in the file. Secrets come from the file's environment references and are stored encrypted."
    )

    def add_arguments(self, parser):
        parser.add_argument("--from-config", required=True, help="Path to the private providers JSON.")
        parser.add_argument("--dry-run", action="store_true", help="Report what would be imported; write nothing.")

    def handle(self, *args, from_config, dry_run, **options):
        from django.conf import settings

        from skeleton.access.models import Role
        from skeleton.accounts.identity_providers import create_identity_provider
        from skeleton.accounts.models import IdentityProvider
        from skeleton.accounts.providers import load_provider_config
        from skeleton.tenancy.models import IdentityProviderAdmission, Organization

        try:
            configuration = load_provider_config(from_config, debug=settings.DEBUG)
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from exc
        imported, skipped = [], []
        try:
            with transaction.atomic():
                for binding in configuration.bindings:
                    if binding.protocol not in ("oidc", "saml"):
                        skipped.append(f"{binding.provider_id}: {binding.protocol} stays in the file")
                        continue
                    if IdentityProvider.all_objects.filter(provider_id=binding.provider_id).exists():
                        skipped.append(f"{binding.provider_id}: already in the database")
                        continue
                    organization = Organization.objects.filter(slug=binding.organization_slug, is_active=True).first()
                    if organization is None:
                        raise CommandError(f"{binding.provider_id}: active organization does not exist.")
                    with system_context(reason=REASON):
                        try:
                            row = create_identity_provider(actor=None, data=_row_data(binding, organization))
                        except ValidationError as exc:
                            raise CommandError(f"{binding.provider_id}: {'; '.join(exc.messages)}") from exc
                    if row.fingerprint != binding.fingerprint:
                        raise CommandError(f"{binding.provider_id}: fingerprint would change; nothing imported.")
                    with system_context(reason=REASON, organization_id=organization.pk):
                        role = Role.objects.filter(code=binding.default_role_code).first()
                        if role is None:
                            raise CommandError(f"{binding.provider_id}: default role does not exist.")
                        IdentityProviderAdmission(
                            organization=organization,
                            provider=row,
                            jit_provisioning=binding.jit_provisioning,
                            default_role=role,
                        ).save()
                    imported.append(binding.provider_id)
                if dry_run:
                    raise _DryRun
        except _DryRun:
            pass
        for line in skipped:
            self.stdout.write(f"skipped {line}")
        verb = "would import" if dry_run else "imported"
        self.stdout.write(f"{verb} {len(imported)}: {', '.join(imported) or '-'}")
