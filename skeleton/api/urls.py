from django.urls import path

from skeleton.api import views
from skeleton.api.invitation_views import ResendInvitationView
from skeleton.files.api_views import AttachmentDetailView, AttachmentRestoreView, AttachmentsView

app_name = "api"
urlpatterns = [
    path("me/", views.MeView.as_view(), name="me"),
    path("organizations/", views.OrganizationsView.as_view(), name="organizations"),
    path("o/<uuid:organization_id>/catalog/", views.CatalogView.as_view(), name="catalog"),
    path("o/<uuid:organization_id>/audit/", views.AuditView.as_view(), name="audit"),
    path("o/<uuid:organization_id>/audit/<uuid:sysid>/", views.AuditView.as_view(), name="audit-detail"),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/references/<str:field_name>/",
        views.ReferenceView.as_view(),
        name="reference-picker",
    ),
    path("o/<uuid:organization_id>/tables/<str:table_key>/records/", views.RecordsView.as_view(), name="records"),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/records/<uuid:sysid>/",
        views.RecordsView.as_view(),
        name="record",
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/records/<uuid:sysid>/restore/",
        views.RestoreView.as_view(),
        name="restore",
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/records/<uuid:sysid>/access/",
        views.RecordAccessView.as_view(),
        name="record-access",
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/records/<uuid:sysid>/attachments/",
        AttachmentsView.as_view(),
        name="attachments",
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/records/<uuid:sysid>/attachments/<uuid:attachment_id>/",
        AttachmentDetailView.as_view(),
        name="attachment",
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/records/<uuid:sysid>/attachments/<uuid:attachment_id>/restore/",
        AttachmentRestoreView.as_view(),
        name="attachment-restore",
    ),
    path(
        "o/<uuid:organization_id>/invitations/<uuid:sysid>/resend/",
        ResendInvitationView.as_view(),
        name="invitation-resend",
    ),
    path("o/<uuid:organization_id>/<str:resource>/", views.DedicatedView.as_view(), name="access-list"),
    path("o/<uuid:organization_id>/<str:resource>/<uuid:sysid>/", views.DedicatedView.as_view(), name="access-detail"),
]
