# Management command: python manage.py send_reminders
# Finds documents that need a renewal reminder and prints a report.
# This keeps the reminder logic in one place; a future version could
# turn the printed lines into real email / WhatsApp notifications.

from django.core.management.base import BaseCommand
from django.utils import timezone

from documents.models import Document


class Command(BaseCommand):
    help = "Print a reminder report for documents near expiry."

    def handle(self, *args, **options):
        today = timezone.localdate()
        documents = Document.objects.all().order_by("expiry_date")

        urgent = []    # expired or expiring within 7 days
        soon = []      # expiring within 30 days (after the 7 day window)

        for document in documents:
            days = document.days_left()
            if days is None:
                continue
            if days < 7:
                urgent.append(document)
            elif days <= 30:
                soon.append(document)

        self.stdout.write("RenewMate reminder report - {}".format(today))
        self.stdout.write("=" * 48)

        if not urgent and not soon:
            self.stdout.write(self.style.SUCCESS("Nothing needs attention."))
            return

        if urgent:
            self.stdout.write(self.style.ERROR("URGENT (expired or within 7 days):"))
            for document in urgent:
                self.stdout.write(
                    "  {} (owner: {}) - {}".format(
                        document.title,
                        document.owner.username,
                        document.get_days_text(),
                    )
                )

        if soon:
            self.stdout.write(self.style.WARNING("UPCOMING (within 30 days):"))
            for document in soon:
                self.stdout.write(
                    "  {} (owner: {}) - {}".format(
                        document.title,
                        document.owner.username,
                        document.get_days_text(),
                    )
                )

        self.stdout.write(
            "Summary: {} urgent, {} upcoming.".format(len(urgent), len(soon))
        )
