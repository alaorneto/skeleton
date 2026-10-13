from django.urls import path
from .application_views import ApplicationAPI, application_list, application_configuration

urlpatterns = [
    path('o/<uuid:organization_id>/apps/', application_list, name='application-list'),
    path('o/<uuid:organization_id>/apps/<str:app_label>/configuration/', application_configuration, name='application-configuration'),
    path('api/v1/o/<uuid:organization_id>/apps/', ApplicationAPI.as_view(), name='api-application-list'),
    path('api/v1/o/<uuid:organization_id>/apps/<str:app_label>/configuration/', ApplicationAPI.as_view(), name='api-application-configuration'),
]
