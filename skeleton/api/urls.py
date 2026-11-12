from django.urls import path

from skeleton.api import views
from skeleton.api.invitation_views import ResendInvitationView
from skeleton.comments.api_views import CommentDetailView, CommentRestoreView, CommentsView, MentionableView
from skeleton.files.api_views import AttachmentDetailView, AttachmentRestoreView, AttachmentsView
from skeleton.tenancy.aggregate import aggregate_route

app_name = "api"
urlpatterns = [
    # Reads each of the actor's organizations in its own context (#74, #178).
    path("openapi.json", aggregate_route(views.OpenAPIView.as_view()), name="openapi"),
    path("me/", views.MeView.as_view(), name="me"),
    path("organizations/", views.OrganizationsView.as_view(), name="organizations"),
    path("o/<uuid:organization_id>/catalog/", views.CatalogView.as_view(), name="catalog"),
    path("o/<uuid:organization_id>/membership/", views.OwnMembershipView.as_view(), name="own-membership"),
    path(
        "o/<uuid:organization_id>/membership/leave/",
        views.LeaveOrganizationView.as_view(),
        name="leave-organization",
    ),
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
        "o/<uuid:organization_id>/tables/<str:table_key>/records/<uuid:sysid>/comments/",
        CommentsView.as_view(),
        name="comments",
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/records/<uuid:sysid>/comments/mentionable/",
        MentionableView.as_view(),
        name="comment-mentionable",
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/records/<uuid:sysid>/comments/<uuid:comment_id>/",
        CommentDetailView.as_view(),
        name="comment",
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/records/<uuid:sysid>/comments/<uuid:comment_id>/restore/",
        CommentRestoreView.as_view(),
        name="comment-restore",
    ),
    path(
        "o/<uuid:organization_id>/invitations/<uuid:sysid>/resend/",
        ResendInvitationView.as_view(),
        name="invitation-resend",
    ),
    path("o/<uuid:organization_id>/<str:resource>/", views.DedicatedView.as_view(), name="access-list"),
    path("o/<uuid:organization_id>/<str:resource>/<uuid:sysid>/", views.DedicatedView.as_view(), name="access-detail"),
]
