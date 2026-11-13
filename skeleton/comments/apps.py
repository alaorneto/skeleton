from django.apps import AppConfig


class CommentsConfig(AppConfig):
    name = "skeleton.comments"
    label = "sk_comments"
    verbose_name = "Comentários"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from skeleton.web.timeline import register_timeline_source

        from .timeline import comment_items

        # Each comment is read through the target's authorization, like every other source.
        register_timeline_source("comments", comment_items)
        from .mentions import register_mention_type

        register_mention_type()
