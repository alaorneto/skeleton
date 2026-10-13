"""Project routing overlay; no core routes need to be copied into a derivative."""
from skeleton.conf.urls import application_urls

urlpatterns = application_urls("config.urls_project")
