from django.urls import path, register_converter

from skeleton.tenancy.routing import ORG

from . import api_views, views


class InstanceActionConverter:
    """Only the instance actions of the API (#479: was a ``re_path`` group); any other text is a 404."""

    regex = "|".join(api_views.INSTANCE_ACTIONS)

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(InstanceActionConverter, "flowaction")
app_name = "flows"
urlpatterns = [
    path("flows/tasks/", views.global_tasks, name="global-tasks"),
    path(f"{ORG}flows/tasks/", views.task_list, name="tasks"),
    path(f"{ORG}flows/tasks/<uuid:sysid>/", views.task_detail, name="task"),
    path(f"{ORG}flows/operations/", views.operations, name="operations"),
    path(f"{ORG}flows/drafts/", views.draft_list, name="drafts"),
    path(f"{ORG}flows/drafts/<uuid:sysid>/", views.draft_detail, name="draft"),
    path(f"{ORG}flows/instances/", views.instance_list, name="instances"),
    path(f"{ORG}flows/instances/<uuid:sysid>/", views.instance_detail, name="instance"),
    path(f"{ORG}flows/instances/<uuid:sysid>/<flowaction:action>/", views.instance_action, name="instance-action"),
    path("api/v1/o/<uuid:organization_id>/flows/tasks/", api_views.TasksView.as_view(), name="api-tasks"),
    path("api/v1/o/<uuid:organization_id>/flows/tasks/<uuid:sysid>/", api_views.TaskView.as_view(), name="api-task"),
    # One literal route per action, so each one is documented in the OpenAPI schema (#178).
    *(
        path(
            f"api/v1/o/<uuid:organization_id>/flows/tasks/<uuid:sysid>/{action}/",
            view.as_view(),
            {"action": action},
            name="api-task-action",
        )
        for action, view in api_views.TASK_ACTION_VIEWS.items()
    ),
    path(
        "api/v1/o/<uuid:organization_id>/flows/operations/", api_views.OperationsView.as_view(), name="api-operations"
    ),
    path("api/v1/o/<uuid:organization_id>/flows/absences/", api_views.AbsencesView.as_view(), name="api-absences"),
    path(
        "api/v1/o/<uuid:organization_id>/flows/absences/<uuid:sysid>/",
        api_views.AbsenceView.as_view(),
        name="api-absence",
    ),
    path("api/v1/o/<uuid:organization_id>/flows/drafts/", api_views.DraftsView.as_view(), name="api-drafts"),
    path(
        "api/v1/o/<uuid:organization_id>/flows/migrations/<str:flow_key>/<int:from_version>/",
        api_views.MigrationView.as_view(),
        name="api-migration",
    ),
    path("api/v1/o/<uuid:organization_id>/flows/drafts/<uuid:sysid>/", api_views.DraftView.as_view(), name="api-draft"),
    path(
        "api/v1/o/<uuid:organization_id>/flows/drafts/<uuid:sysid>/publish/",
        api_views.DraftPublishView.as_view(),
        name="api-draft-publish",
    ),
    path("api/v1/o/<uuid:organization_id>/flows/instances/", api_views.InstancesView.as_view(), name="api-instances"),
    path(
        "api/v1/o/<uuid:organization_id>/flows/instances/<uuid:sysid>/",
        api_views.InstanceView.as_view(),
        name="api-instance",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/flows/instances/<uuid:sysid>/history/",
        api_views.InstanceHistoryView.as_view(),
        name="api-instance-history",
    ),
    *(
        path(
            f"api/v1/o/<uuid:organization_id>/flows/instances/<uuid:sysid>/{action}/",
            view.as_view(),
            {"action": action},
            name="api-instance-action",
        )
        for action, view in api_views.INSTANCE_ACTION_VIEWS.items()
    ),
]
