import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('sk_tenancy', '0001_initial'),
        ('sk_access', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Invitation',
            fields=[
                ('sysid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('updated_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('created_by', models.UUIDField(blank=True, editable=False, null=True)),
                ('updated_by', models.UUIDField(blank=True, editable=False, null=True)),
                ('deleted_by', models.UUIDField(blank=True, editable=False, null=True)),
                ('deleted_at', models.DateTimeField(blank=True, editable=False, null=True)),
                ('revision', models.PositiveBigIntegerField(default=1, editable=False)),
                ('email', models.EmailField(max_length=254)),
                ('token_digest', models.CharField(editable=False, max_length=64, unique=True)),
                ('expires_at', models.DateTimeField()),
                ('accepted_at', models.DateTimeField(blank=True, editable=False, null=True)),
                ('accepted_by', models.UUIDField(blank=True, editable=False, null=True)),
                ('role', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='invitations', to='sk_access.role')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='%(app_label)s_%(class)s_records', to='sk_tenancy.organization')),
            ],
            options={
                'abstract': False,
                'base_manager_name': 'all_objects',
                'default_manager_name': 'objects',
                'constraints': [models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='sk_tenancy_invitation_revision_positive'), models.CheckConstraint(condition=models.Q(('deleted_by__isnull', True), ('deleted_at__isnull', False), _connector='OR'), name='sk_tenancy_invitation_deleted_actor'), models.UniqueConstraint(fields=('organization', 'sysid'), name='sk_tenancy_invitation_org_sysid', violation_error_code='unique'), models.CheckConstraint(condition=models.Q(models.Q(('accepted_at__isnull', True), ('accepted_by__isnull', True)), models.Q(('accepted_at__isnull', False), ('accepted_by__isnull', False)), _connector='OR'), name='sk_tenancy_invitation_acceptance')],
            },
            managers=[
                ('objects', django.db.models.manager.Manager()),
                ('all_objects', django.db.models.manager.Manager()),
            ],
        ),
    ]
