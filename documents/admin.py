# Admin registration for the documents app.

from django.contrib import admin

from .models import Document, DocumentGroup, Renewal


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "kind", "group", "expiry_date")
    list_filter = ("kind",)
    search_fields = ("title", "document_number", "owner__username")


@admin.register(DocumentGroup)
class DocumentGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "created_at")
    search_fields = ("name", "owner__username")


@admin.register(Renewal)
class RenewalAdmin(admin.ModelAdmin):
    list_display = ("document", "old_expiry_date", "new_expiry_date", "renewed_on")
    list_filter = ("renewed_on",)
    search_fields = ("document__title", "document__owner__username")

