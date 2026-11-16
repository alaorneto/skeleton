"""Core routes of the host project; an app's own routes come from its manifest (runtime.urls)."""

from skeleton.conf.urls import application_urls

urlpatterns = application_urls()
