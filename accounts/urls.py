# URL patterns for the accounts app.

from django.urls import path

from . import views

urlpatterns = [
    path("register/", views.register, name="register"),
    path("login/", views.RenewMateLoginView.as_view(), name="login"),
    path("logout/", views.RenewMateLogoutView.as_view(), name="logout"),
    path("profile/", views.profile, name="profile"),
    path("profile/edit/", views.profile_edit, name="profile_edit"),
]
