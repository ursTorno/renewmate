# Root URL configuration for the RenewMate project.
# Each app keeps its own urls.py; this file only routes to them.

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Landing page + profile
    path("", include("dashboard.urls")),
    # Registration, login, logout
    path("accounts/", include("accounts.urls")),
    # The main feature: document management
    path("documents/", include("documents.urls")),
]

# Serve uploaded files through the dev server when DEBUG is on.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
