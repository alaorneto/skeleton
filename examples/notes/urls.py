from django.urls import path
from .views import home

app_name = 'example_notes'
urlpatterns = [path('', home, name='home')]
