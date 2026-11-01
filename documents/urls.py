# URL patterns for the documents app.

from django.urls import path

from . import views

urlpatterns = [
    path("", views.document_list, name="document_list"),
    path("add/", views.document_create, name="document_create"),
    # User-created groups (folders) for related documents.
    path("groups/", views.group_list, name="group_list"),
    path("groups/add/", views.group_create, name="group_create"),
    path("groups/<int:pk>/", views.group_detail, name="group_detail"),
    path(
        "groups/<int:pk>/add-documents/",
        views.group_add_documents,
        name="group_add_documents",
    ),
    path("groups/<int:pk>/edit/", views.group_update, name="group_update"),
    path("groups/<int:pk>/delete/", views.group_delete, name="group_delete"),
    path("<int:pk>/", views.document_detail, name="document_detail"),
    path("<int:pk>/edit/", views.document_update, name="document_update"),
    path("<int:pk>/delete/", views.document_delete, name="document_delete"),
    path("<int:pk>/renew/", views.document_renew, name="document_renew"),
]

