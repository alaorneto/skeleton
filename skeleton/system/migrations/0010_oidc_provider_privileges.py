"""Runtime privileges on the tables of the OIDC provider for native clients (#603)."""

from django.db import migrations

from skeleton.common.technical_operations import GrantTechnicalTablePrivileges


class Migration(migrations.Migration):
    dependencies = [
        ("sk_system", "0009_closed_defaults"),
        ("allauth_idp_oidc", "0003_client_allow_uri_wildcards"),
    ]
    operations = [
        GrantTechnicalTablePrivileges(
            {
                # The operator's command registers clients; the provider issues, rotates and deletes tokens.
                "allauth_idp_oidc_client": ("SELECT", "INSERT", "UPDATE"),
                "allauth_idp_oidc_token": ("SELECT", "INSERT", "UPDATE", "DELETE"),
            }
        )
    ]
