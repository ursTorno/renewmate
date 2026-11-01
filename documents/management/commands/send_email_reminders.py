# Management command: python manage.py send_email_reminders
# Sends ONE email per user listing every document that needs a renewal
# reminder (expired, due within 7 days, due within 30 days).
#
# Development (prints to the terminal, nothing is really sent):
#     python manage.py send_email_reminders
#
# Real SMTP (Gmail example):
#     EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend \
#     EMAIL_HOST=smtp.gmail.com EMAIL_PORT=587 EMAIL_USE_TLS=True \
#     EMAIL_HOST_USER=you@gmail.com EMAIL_HOST_PASSWORD="app-password" \
#     python manage.py send_email_reminders
#
# The "Log in to RenewMate" link uses settings.BASE_URL, which defaults to
# http://127.0.0.1:8000 (local development / demo). Override it with the
# RENEWMATE_BASE_URL environment variable.

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from documents.models import Document


class Command(BaseCommand):
    help = "Email every user a summary of documents that need renewal."

    def handle(self, *args, **options):
        today = timezone.localdate()
        documents = Document.objects.all().order_by("expiry_date")

        # Group the documents that need attention by their owner.
        by_user = {}
        for document in documents:
            days = document.days_left()
            if days is None or days > 30:
                continue
            by_user.setdefault(document.owner, []).append(document)

        emails_sent = 0
        for owner, docs in by_user.items():
            if not owner.email:
                self.stdout.write("Skipping {} - no email address".format(owner.username))
                continue

            lines = [
                "  - {} ({}) - {}".format(
                    d.title, d.get_kind_display(), d.get_days_text()
                )
                for d in docs
            ]
            subject = "RenewMate: {} document(s) need attention".format(len(docs))
            message = (
                "Hi {},\n\n"
                "The following documents need your attention:\n\n"
                "{}\n\n"
                "Log in to RenewMate to renew them: {}\n\n"
                "-- RenewMate, never miss a renewal."
            ).format(
                owner.username,
                "\n".join(lines),
                settings.BASE_URL.rstrip("/"),
            )

            send_mail(subject, message, None, [owner.email])
            self.stdout.write(
                self.style.SUCCESS(
                    "Emailed {} <{}> ({} document(s))".format(owner.username, owner.email, len(docs))
                )
            )
            emails_sent += 1

        self.stdout.write("Done. Emails sent: {}".format(emails_sent))