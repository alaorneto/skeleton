"""Create missing builtin roles in every active organization, one transaction each."""

from django.core.management.base import BaseCommand, CommandError

from skeleton.access.services import reconcile_builtin_roles


class Command(BaseCommand):
    help = "Cria os papéis internos que faltam nas organizações ativas e reporta colisões de código."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")

    def handle(self, *args, database, **options):
        from skeleton.tenancy.models import Organization

        # Materialize IDs first: no global transaction wraps the per-organization loop.
        organization_ids = list(
            Organization.all_objects.using(database)
            .filter(is_active=True, deleted_at__isnull=True)
            .order_by("sysid")
            .values_list("sysid", flat=True)
        )
        collisions = {}
        for organization_id in organization_ids:
            found = reconcile_builtin_roles(organization_id=organization_id, using=database)
            if found:
                collisions[organization_id] = found
        for organization_id, codes in collisions.items():
            self.stderr.write(f"{organization_id}: papel customizado usa código reservado: {', '.join(codes)}")
        self.stdout.write(f"Organizações reconciliadas: {len(organization_ids)}; colisões: {len(collisions)}.")
        if collisions:
            raise CommandError("Renomeie os papéis customizados com códigos reservados e repita.")
