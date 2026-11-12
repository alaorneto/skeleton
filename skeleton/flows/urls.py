from django.urls import path, re_path

from . import api_views, views

app_name = "flows"
TASK_ACTIONS = "|".join(api_views.TASK_ACTIONS)
INSTANCE_ACTIONS = "|".join(api_views.INSTANCE_ACTIONS)
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
urlpatterns = [
    path("o/<uuid:organization_id>/flows/tasks/", views.task_list, name="tasks"),
    path("o/<uuid:organization_id>/flows/tasks/<uuid:sysid>/", views.task_detail, name="task"),
    path("o/<uuid:organization_id>/flows/instances/", views.instance_list, name="instances"),
    path("o/<uuid:organization_id>/flows/instances/<uuid:sysid>/", views.instance_detail, name="instance"),
    re_path(
        rf"^o/(?P<organization_id>{UUID})/flows/instances/(?P<sysid>{UUID})/(?P<action>{INSTANCE_ACTIONS})/$",
        views.instance_action,
        name="instance-action",
    ),
    path("api/v1/o/<uuid:organization_id>/flows/tasks/", api_views.TasksView.as_view(), name="api-tasks"),
    path("api/v1/o/<uuid:organization_id>/flows/tasks/<uuid:sysid>/", api_views.TaskView.as_view(), name="api-task"),
    re_path(
        rf"^api/v1/o/(?P<organization_id>{UUID})/flows/tasks/(?P<sysid>{UUID})/(?P<action>{TASK_ACTIONS})/$",
        api_views.TaskActionView.as_view(),
        name="api-task-action",
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
    re_path(
        rf"^api/v1/o/(?P<organization_id>{UUID})/flows/instances/(?P<sysid>{UUID})/(?P<action>{INSTANCE_ACTIONS})/$",
        api_views.InstanceActionView.as_view(),
        name="api-instance-action",
    ),
]
