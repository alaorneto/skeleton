from django.urls import path
from .views import SummaryView

app_name = 'example_notes_api'
urlpatterns = [path('summary/', SummaryView.as_view(), name='summary')]
