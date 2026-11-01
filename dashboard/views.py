# Views for the dashboard app: public landing page + logged-in home page.
# The dashboard summarises the user's documents and what needs attention.

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from documents.models import Document


def home(request):
    """Public landing page for visitors."""
    return render(request, "home.html")


@login_required
def dashboard(request):
    """Personal dashboard: stats cards plus the documents needing attention."""
    documents = Document.objects.filter(owner=request.user)

    # Status is calculated from expiry_date (see Document.status()).
    valid = []
    expiring = []
    expired = []
    for document in documents:
        if document.status() == "valid":
            valid.append(document)
        elif document.status() == "expiring":
            expiring.append(document)
        elif document.status() == "expired":
            expired.append(document)

    # Expiring soon = expiring within 30 days, nearest expiry first.
    expiring.sort(key=lambda d: d.expiry_date)

    context = {
        "total": documents.count(),
        "valid_count": len(valid),
        "expiring_count": len(expiring),
        "expired_count": len(expired),
        "expiring": expiring,
        "recent": documents.order_by("-created_at")[:5],
    }
    return render(request, "dashboard.html", context)


@login_required
def reminders(request):
    """Reminder page: expired documents and everything due within 30 days.

    Reuses the same expiry logic as everywhere else (Document.days_left()),
    just grouped into the three reminder buckets.
    """
    documents = Document.objects.filter(owner=request.user)

    expired = []   # expiry date already passed
    urgent = []    # expires within the next 7 days
    upcoming = []  # expires in 8-30 days

    for document in documents:
        days = document.days_left()
        if days is None:
            continue
        if days < 0:
            expired.append(document)
        elif days <= 7:
            urgent.append(document)
        elif days <= 30:
            upcoming.append(document)

    expired.sort(key=lambda d: d.expiry_date)
    urgent.sort(key=lambda d: d.expiry_date)
    upcoming.sort(key=lambda d: d.expiry_date)

    context = {
        "expired": expired,
        "urgent": urgent,
        "upcoming": upcoming,
        "attention_count": len(expired) + len(urgent) + len(upcoming),
    }
    return render(request, "reminders.html", context)

