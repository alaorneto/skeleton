from django.apps import AppConfig


class TransferConfig(AppConfig):
    name = "skeleton.transfer"
    label = "sk_transfer"
    verbose_name = "Importação e exportação"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from datetime import timedelta

        from skeleton.jobs.periodic import register_periodic
        from skeleton.jobs.registry import JobType, register_job_type
        from skeleton.notifications.registry import NotificationType, register_notification_type

        from .exports import EXPORT_JOB, NOTIFICATION, run_export, transfer_url
        from .retention import RETENTION_JOB, purge_expired_transfers

        # run_as="actor": the worker re-enters the requester's tenant context, so a revoked
        # membership fails closed before the handler reads anything.
        register_job_type(
            JobType(
                EXPORT_JOB,
                run_export,
                {"transfer_id": "uuid"},
                run_as="actor",
                max_attempts=3,
                timeout_s=1800,
                owner_app="sk_transfer",
            )
        )
        register_job_type(
            JobType(
                RETENTION_JOB, purge_expired_transfers, {}, run_as="system", max_attempts=3, owner_app="sk_transfer"
            )
        )
        register_periodic(RETENTION_JOB, RETENTION_JOB, every=timedelta(hours=1))
        register_notification_type(
            NotificationType(
                NOTIFICATION,
                "Exportação concluída",
                payload_fields=("transfer", "state"),
                mandatory=True,
                owner_app="sk_transfer",
                open_url=transfer_url,
            )
        )
