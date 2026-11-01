# URL patterns for the dashboard app.

from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("reminders/", views.reminders, name="reminders"),
]


