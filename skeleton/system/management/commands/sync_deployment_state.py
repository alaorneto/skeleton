import json
from pathlib import Path
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from skeleton.system.models import AppInstallation, DeploymentOperation
from skeleton.system.releases import DeploymentUnavailable, reconcile_operation, synchronize_inventory
from skeleton.tenancy.context import system_context
from skeleton.tenancy.models import Organization


class Command(BaseCommand):
    help = 'Reconcile trusted agent inventory/journal into audited global deployment state.'

    def add_arguments(self, parser):
        parser.add_argument('--inventory', type=Path, required=True)
        parser.add_argument('--operation', type=Path)
        parser.add_argument('--database', default='default')

    def handle(self, *args, **options):
        using = options['database']
        try:
            inventory = json.loads(options['inventory'].read_text())
            with system_context(reason='deployment:reconcile', using=using):
                components = synchronize_inventory(inventory, using=using)
                if options['operation']:
                    status = json.loads(options['operation'].read_text())
                    identifier = UUID(str(status['id']))
                    row = DeploymentOperation.objects.using(using).filter(pk=identifier).first()
                    if row is None:
                        plan = status['plan']
                        row = DeploymentOperation(
                            sysid=identifier, operation=status.get('action', status.get('operation')),
                            expected_generation=plan['generation'], plan_digest=status.get('plan_digest', plan['digest']),
                            plan=plan,
                        )
                        row.save(using=using)
                    reconcile_operation(row, status)
                disabled = [item['app_label'] for item in components
                            if item['kind'] == 'app' and item.get('state') == 'schema_retained']
                org_ids = list(Organization.all_objects.using(using).values_list('sysid', flat=True))
            for organization_id in org_ids:
                # A separate transaction per organization, including when runtime RLS is enabled.
                with system_context(reason='deployment:deactivate', organization_id=organization_id, using=using):
                    AppInstallation.all_objects.using(using).filter(organization_id=organization_id, app_label__in=disabled, enabled=True).update(enabled=False)
        except (DeploymentUnavailable, ValueError, OSError, KeyError, TypeError) as exc:
            raise CommandError(f'Deployment reconciliation failed: {type(exc).__name__}') from None
        self.stdout.write(self.style.SUCCESS('Deployment inventory reconciled.'))
