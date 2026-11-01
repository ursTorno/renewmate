# Small template helpers for the documents app.

from django import template

register = template.Library()

# Bootstrap 5 colour used for each status badge.
STATUS_BADGE = {
    "valid": "success",
    "expiring": "warning",
    "expired": "danger",
    "unknown": "secondary",
}

# Bootstrap icon shown next to each document kind.
KIND_ICON = {
    "ID": "bi-person-vcard",
    "CERT": "bi-award",
    "LIC": "bi-card-checklist",
    "PASSPORT": "bi-airplane",
    "OTHER": "bi-file-earmark-text",
}


@register.simple_tag
def status_badge(status):
    """Usage in a template: {% status_badge document.status %}"""
    return STATUS_BADGE.get(status, "secondary")


@register.simple_tag
def kind_icon(kind):
    """Usage in a template: {% kind_icon document.kind %}"""
    return KIND_ICON.get(kind, "bi-file-earmark-text")


@register.simple_tag
def days_text(document):
    """Human friendly sentence about expiry (logic lives on the model)."""
    return document.get_days_text()
