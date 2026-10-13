"""Runtime privileges for the pinned libraries, after their schemas exist."""
from django.db import migrations
from skeleton.common.technical_operations import GrantTechnicalTablePrivileges

# Inventory is deliberately frozen and per-table: no default/blanket DML grants.
READ_ONLY = (
    'django_migrations', 'django_content_type', 'auth_permission', 'auth_group',
    'auth_group_permissions', 'socialaccount_socialapp', 'socialaccount_socialtoken',
)
MUTABLE = (
    'django_session', 'account_emailaddress', 'account_emailconfirmation',
    'socialaccount_socialaccount', 'mfa_authenticator',
)




class Migration(migrations.Migration):
    dependencies = [
        ('sk_system', '0001_initial'), ('sk_audit', '0002_record_infrastructure'),
        ('account', '0009_emailaddress_unique_primary_email'),
        ('socialaccount', '0006_alter_socialaccount_extra_data'),
        ('mfa', '0003_authenticator_type_uniq'), ('sessions', '0001_initial'),
        ('admin', '0003_logentry_add_action_flag_choices'),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]
    operations = [GrantTechnicalTablePrivileges({
        **{table: ("SELECT",) for table in READ_ONLY},
        **{table: ("SELECT", "INSERT", "UPDATE", "DELETE") for table in MUTABLE},
        "django_admin_log": ("SELECT", "INSERT"),
    })]
