# Tests for the documents app.
# Run them with:  python manage.py test documents

import datetime
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from io import BytesIO, StringIO
from pathlib import Path

from PIL import Image

from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Document, DocumentGroup, Renewal
from .services.field_extraction import (
    PRIMARY_IDENTIFIER_KEYS,
    extract_driving_license_fields,
    extract_primary_identifier,
    extract_vehicle_insurance_fields,
    extract_vehicle_registration_fields,
)
from .services.ocr_save import (
    SUPPORTED_SAVE_TYPES,
    build_document_values,
    build_ocr_initial,
)

# Uploads made by the tests go to a temporary folder, not the real media dir.
MEDIA_ROOT_TEMP = tempfile.mkdtemp()


def make_document(user, title="Driving License", expiry_days=10, **kwargs):
    """Helper that builds and saves a Document with relative expiry."""
    expiry = timezone.localdate() + datetime.timedelta(days=expiry_days)
    kwargs.setdefault("kind", "LIC")
    return Document.objects.create(
        owner=user, title=title, expiry_date=expiry, **kwargs
    )


def create_user(name):
    from django.contrib.auth.models import User

    return User.objects.create_user(username=name, password="secret123")


class ExpiryStatusTests(TestCase):
    """Check the status / days-left logic, including the edge cases."""

    def setUp(self):
        self.user = create_user("testuser")

    def test_expires_today(self):
        document = make_document(self.user, expiry_days=0)
        self.assertEqual(document.days_left(), 0)
        self.assertEqual(document.status(), "expiring")
        self.assertEqual(document.get_days_text(), "Expires today")

    def test_expires_tomorrow(self):
        document = make_document(self.user, expiry_days=1)
        self.assertEqual(document.days_left(), 1)
        self.assertEqual(document.status(), "expiring")
        self.assertEqual(document.get_days_text(), "Expires in 1 day")

    def test_expires_in_seven_days(self):
        document = make_document(self.user, expiry_days=7)
        self.assertEqual(document.status(), "expiring")
        self.assertEqual(document.get_days_text(), "Expires in 7 days")

    def test_expires_in_exactly_thirty_days(self):
        document = make_document(self.user, expiry_days=30)
        self.assertEqual(document.status(), "expiring")

    def test_expires_in_thirty_one_days_is_valid(self):
        document = make_document(self.user, expiry_days=31)
        self.assertEqual(document.status(), "valid")
        self.assertEqual(document.get_days_text(), "Expires in 31 days")

    def test_already_expired(self):
        document = make_document(self.user, expiry_days=-12)
        self.assertEqual(document.days_left(), -12)
        self.assertEqual(document.status(), "expired")
        self.assertEqual(document.get_days_text(), "Expired 12 days ago")

    def test_no_expiry_date(self):
        document = Document.objects.create(owner=self.user, title="Old Card")
        self.assertIsNone(document.days_left())
        self.assertEqual(document.status(), "unknown")
        self.assertEqual(document.get_days_text(), "No expiry date set")


@override_settings(MEDIA_ROOT=MEDIA_ROOT_TEMP)
class DocumentViewTests(TestCase):
    """CRUD views, permissions, search and filters."""

    def setUp(self):
        self.user = create_user("alice")
        self.other = create_user("bob")
        self.client.login(username="alice", password="secret123")
        self.document = make_document(self.user, title="My License")

    def test_views_require_login(self):
        self.client.logout()
        response = self.client.get(reverse("document_list"))
        self.assertEqual(response.status_code, 302)  # redirected to login
        self.assertIn("/accounts/login/", response.url)

    def test_document_list_shows_own_documents(self):
        response = self.client.get(reverse("document_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My License")

    def test_user_cannot_open_foreign_document(self):
        foreign = make_document(self.other, title="Bob License")
        response = self.client.get(reverse("document_detail", args=[foreign.pk]))
        self.assertEqual(response.status_code, 404)

    def test_user_cannot_edit_foreign_document(self):
        foreign = make_document(self.other)
        response = self.client.get(reverse("document_update", args=[foreign.pk]))
        self.assertEqual(response.status_code, 404)

    def test_create_document(self):
        response = self.client.post(
            reverse("document_create"),
            {
                "title": "Passport",
                "kind": "PASSPORT",
                "document_number": "Z1234567",
                "issuing_authority": "Consulate",
                "issue_date": "2020-01-01",
                "expiry_date": "2030-01-01",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Document.objects.filter(owner=self.user, title="Passport").exists()
        )

    def test_edit_document(self):
        response = self.client.post(
            reverse("document_update", args=[self.document.pk]),
            {
                "title": "Renewed License",
                "kind": "LIC",
                "document_number": "KA05",
                "issuing_authority": "RTO",
                "issue_date": "2020-01-01",
                "expiry_date": "2030-01-01",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.document.refresh_from_db()
        self.assertEqual(self.document.title, "Renewed License")

    def test_delete_document(self):
        response = self.client.post(reverse("document_delete", args=[self.document.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Document.objects.filter(pk=self.document.pk).exists())

    def test_delete_page_asks_for_confirmation(self):
        response = self.client.get(reverse("document_delete", args=[self.document.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delete this document?")

    def test_search_by_title(self):
        make_document(self.user, title="Health Card")
        response = self.client.get(reverse("document_list"), {"q": "health"})
        self.assertContains(response, "Health Card")
        self.assertNotContains(response, "My License")

    def test_filter_by_status(self):
        make_document(self.user, title="Valid Doc", expiry_days=200)
        response = self.client.get(reverse("document_list"), {"status": "expiring"})
        self.assertContains(response, "My License")
        self.assertNotContains(response, "Valid Doc")

    def test_filter_by_kind(self):
        make_document(self.user, title="Some ID", kind="ID", expiry_days=200)
        response = self.client.get(reverse("document_list"), {"kind": "ID"})
        self.assertContains(response, "Some ID")
        self.assertNotContains(response, "My License")

    def test_file_upload_and_invalid_extension(self):
        # A valid upload (png extension allowed).
        upload = SimpleUploadedFile("scan.png", b"fakepng", content_type="image/png")
        response = self.client.post(
            reverse("document_create"),
            {
                "title": "Insurance",
                "kind": "OTHER",
                "expiry_date": "2030-01-01",
                "file": upload,
            },
        )
        self.assertEqual(response.status_code, 302)
        saved = Document.objects.get(title="Insurance")
        self.assertTrue(saved.file)

        # A forbidden extension is rejected and the form is shown again.
        bad = SimpleUploadedFile("virus.exe", b"xx", content_type="application/zip")
        response = self.client.post(
            reverse("document_create"),
            {
                "title": "Bad File",
                "kind": "OTHER",
                "expiry_date": "2030-01-01",
                "file": bad,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Document.objects.filter(title="Bad File").exists())

    def test_expiry_before_issue_rejected(self):
        response = self.client.post(
            reverse("document_create"),
            {
                "title": "Broken",
                "kind": "OTHER",
                "issue_date": "2022-01-01",
                "expiry_date": "2021-01-01",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Expiry date must be on or after")
        self.assertFalse(Document.objects.filter(title="Broken").exists())


# ----------------------------------------------------------------------
# Editing the Document Type of an existing document
# ----------------------------------------------------------------------
# The Edit Document page shows the model's existing KIND_CHOICES select for
# every document, so a document stored as "Other" can be corrected to any
# real type. Changing the type is a plain form edit: OCR never runs, and the
# other fields, the file, the renewal history and the computed reminder
# status stay untouched. Insurance and RC documents are stored as "Other"
# in this app, so the dropdown intentionally offers exactly the model's
# existing choices - no invented document types.
# ----------------------------------------------------------------------


@override_settings(MEDIA_ROOT=MEDIA_ROOT_TEMP)
class EditDocumentTypeTests(TestCase):
    """A document saved as "Other" can be re-typed from the Edit page."""

    def setUp(self):
        self.user = create_user("owner")
        self.other = create_user("someoneelse")
        self.client.login(username="owner", password="secret123")
        self.scan = SimpleUploadedFile(
            "scan.png", b"fake image bytes", content_type="image/png"
        )
        self.document = make_document(
            self.user,
            title="Insurance policy",
            expiry_days=100,
            kind="OTHER",
            document_number="P-12345/2024",
            issuing_authority="ABC Insurance",
            issue_date=datetime.date(2025, 1, 1),
            notes="Policy Holder: ANITA SHARMA",
            file=self.scan,
        )
        self.expiry = self.document.expiry_date
        self.original_file_name = self.document.file.name

    def tearDown(self):
        shutil.rmtree(MEDIA_ROOT_TEMP, ignore_errors=True)
        os.makedirs(MEDIA_ROOT_TEMP, exist_ok=True)

    def edit_data(self, kind="OTHER", **overrides):
        """The POST the edit form sends, with the chosen type."""
        data = {
            "title": self.document.title,
            "kind": kind,
            "document_number": self.document.document_number,
            "issuing_authority": self.document.issuing_authority,
            "issue_date": self.document.issue_date.strftime("%Y-%m-%d"),
            "expiry_date": self.expiry.strftime("%Y-%m-%d"),
            "notes": self.document.notes,
        }
        data.update(overrides)
        return data

    # ------------------------------------------------ the dropdown itself
    def test_edit_page_offers_the_existing_document_types(self):
        response = self.client.get(
            reverse("document_update", args=[self.document.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<select name="kind"')
        for kind, _label in Document.KIND_CHOICES:
            self.assertContains(response, 'value="{}"'.format(kind))
        # The current type ("Other") is the selected one.
        self.assertContains(response, '<option value="OTHER" selected>')
        # Editing never offers OCR: OCR is an Add Document helper only.
        self.assertNotContains(response, 'name="ocr_action"')

    def test_dropdown_matches_the_model_choices_exactly(self):
        # Insurance / RC documents are stored as "Other" in this app, so the
        # dropdown offers the model's existing choices - no invented types.
        self.assertEqual(
            [kind for kind, _label in Document.KIND_CHOICES],
            ["ID", "CERT", "LIC", "PASSPORT", "OTHER"],
        )

    # ------------------------------------------------- change the type
    def test_change_others_to_driving_licence(self):
        response = self.client.post(
            reverse("document_update", args=[self.document.pk]),
            self.edit_data(kind="LIC"),
            follow=True,
        )
        self.assertContains(response, "Document updated successfully.")
        self.document.refresh_from_db()
        self.assertEqual(self.document.kind, "LIC")
        self.assertEqual(self.document.get_kind_display(), "License")

    def test_change_others_to_certificate(self):
        response = self.client.post(
            reverse("document_update", args=[self.document.pk]),
            self.edit_data(kind="CERT"),
            follow=True,
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.kind, "CERT")
        self.assertEqual(self.document.get_kind_display(), "Certificate")

    def test_change_others_to_passport(self):
        response = self.client.post(
            reverse("document_update", args=[self.document.pk]),
            self.edit_data(kind="PASSPORT"),
            follow=True,
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.kind, "PASSPORT")
        self.assertEqual(self.document.get_kind_display(), "Passport")

    def test_change_others_to_id_proof(self):
        response = self.client.post(
            reverse("document_update", args=[self.document.pk]),
            self.edit_data(kind="ID"),
            follow=True,
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.kind, "ID")
        self.assertEqual(self.document.get_kind_display(), "ID Proof")

    # ------------------------------------------ everything else stays
    def test_changing_the_type_keeps_all_other_fields(self):
        self.client.post(
            reverse("document_update", args=[self.document.pk]),
            self.edit_data(kind="LIC"),
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.kind, "LIC")
        self.assertEqual(self.document.title, "Insurance policy")
        self.assertEqual(self.document.document_number, "P-12345/2024")
        self.assertEqual(self.document.issuing_authority, "ABC Insurance")
        self.assertEqual(self.document.issue_date, datetime.date(2025, 1, 1))
        self.assertEqual(self.document.expiry_date, self.expiry)
        self.assertEqual(self.document.notes, "Policy Holder: ANITA SHARMA")
        self.assertEqual(self.document.owner, self.user)
        # The uploaded scan is untouched and still exists in storage.
        self.assertEqual(self.document.file.name, self.original_file_name)
        self.assertTrue(self.document.file.storage.exists(self.document.file.name))
        # Reminders are computed from the expiry date, which did not move.
        self.assertEqual(self.document.days_left(), 100)
        self.assertEqual(self.document.get_days_text(), "Expires in 100 days")

    def test_changing_the_type_keeps_the_renewal_history(self):
        Renewal.objects.create(
            document=self.document,
            old_expiry_date=self.expiry - datetime.timedelta(days=365),
            new_expiry_date=self.expiry,
            notes="Renewed online",
        )
        self.client.post(
            reverse("document_update", args=[self.document.pk]),
            self.edit_data(kind="LIC"),
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.kind, "LIC")
        renewal = self.document.renewals.get()
        self.assertEqual(renewal.notes, "Renewed online")
        self.assertEqual(Renewal.objects.count(), 1)

    # ------------------------------------------------------- security
    def test_another_user_cannot_change_the_type(self):
        self.client.logout()
        self.client.login(username="someoneelse", password="secret123")
        response = self.client.post(
            reverse("document_update", args=[self.document.pk]),
            self.edit_data(kind="LIC", title="Hacked"),
        )
        self.assertEqual(response.status_code, 404)
        self.document.refresh_from_db()
        self.assertEqual(self.document.kind, "OTHER")
        self.assertEqual(self.document.title, "Insurance policy")
        self.assertFalse(Document.objects.filter(owner=self.other).exists())

    # ------------------------------------- manual editing still works
    def test_manual_edit_still_works_together_with_a_type_change(self):
        response = self.client.post(
            reverse("document_update", args=[self.document.pk]),
            self.edit_data(
                kind="LIC",
                title="Driving Licence - RAM",
                document_number="KA0520110001234",
            ),
            follow=True,
        )
        self.assertContains(response, "Document updated successfully.")
        self.document.refresh_from_db()
        self.assertEqual(self.document.kind, "LIC")
        self.assertEqual(self.document.title, "Driving Licence - RAM")
        self.assertEqual(self.document.document_number, "KA0520110001234")
        self.assertEqual(self.document.notes, "Policy Holder: ANITA SHARMA")


class ReminderCommandTests(TestCase):
    """The send_reminders management command output."""

    def setUp(self):
        self.user = create_user("alice")

    def test_report_lists_urgent_and_upcoming(self):
        make_document(self.user, title="Urgent Doc", expiry_days=-1)
        make_document(self.user, title="Soon Doc", expiry_days=20)
        out = StringIO()
        call_command("send_reminders", stdout=out)
        output = out.getvalue()
        self.assertIn("Urgent Doc", output)
        self.assertIn("Soon Doc", output)
        self.assertIn("1 urgent, 1 upcoming", output)

    def test_report_when_nothing_needs_attention(self):
        out = StringIO()
        call_command("send_reminders", stdout=out)
        self.assertIn("Nothing needs attention", out.getvalue())


class RenewalTests(TestCase):
    """The Renewal History feature: creating renewals, updating expiry,
    showing the history and keeping records private to their owner."""

    def setUp(self):
        self.user = create_user("alice")
        self.other = create_user("bob")
        self.client.login(username="alice", password="secret123")
        self.document = make_document(self.user, title="My License", expiry_days=10)
        self.url = reverse("document_renew", args=[self.document.pk])

    def renewal_post(self, **overrides):
        data = {"new_expiry_date": "2030-01-01", "notes": "Renewed online"}
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_renewal_form_renders(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New expiry date")
        self.assertContains(response, "Current expiry date")

    def test_renewal_creates_record_and_updates_expiry(self):
        old_expiry = self.document.expiry_date
        response = self.renewal_post()
        self.assertEqual(response.status_code, 302)  # back to the detail page
        self.assertEqual(Renewal.objects.count(), 1)

        renewal = Renewal.objects.get()
        self.assertEqual(renewal.document, self.document)
        self.assertEqual(renewal.old_expiry_date, old_expiry)
        self.assertEqual(str(renewal.new_expiry_date), "2030-01-01")
        self.assertEqual(renewal.notes, "Renewed online")

        # The document's own expiry date moved forward.
        self.document.refresh_from_db()
        self.assertEqual(self.document.expiry_date, renewal.new_expiry_date)
        self.assertGreater(self.document.expiry_date, old_expiry)

    def test_renewal_history_shown_on_detail_page(self):
        self.renewal_post(new_expiry_date="2031-01-01", notes="Two more years")
        response = self.client.get(reverse("document_detail", args=[self.document.pk]))
        self.assertContains(response, "Renewal History")
        self.assertContains(response, "Old expiry:")
        self.assertContains(response, "01 Jan 2031")
        self.assertContains(response, "Two more years")

    def test_new_expiry_must_be_after_current(self):
        # Current expiry is today + 10 days, so today + 5 must be rejected.
        too_soon = (timezone.localdate() + datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        response = self.renewal_post(new_expiry_date=too_soon)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must be after the current expiry date")
        self.assertEqual(Renewal.objects.count(), 0)
        self.document.refresh_from_db()
        self.assertEqual(self.document.days_left(), 10)  # unchanged

    def test_renewal_on_same_day_rejected(self):
        same_day = self.document.expiry_date.strftime("%Y-%m-%d")
        response = self.renewal_post(new_expiry_date=same_day)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Renewal.objects.count(), 0)

    def test_cannot_renew_foreign_document(self):
        foreign = make_document(self.other)
        response = self.client.post(
            reverse("document_renew", args=[foreign.pk]),
            {"new_expiry_date": "2030-01-01", "notes": ""},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Renewal.objects.count(), 0)

    def test_cannot_view_foreign_renewal_history(self):
        foreign = make_document(self.other)
        # The history lives on the detail page, which 404s for other owners.
        response = self.client.get(reverse("document_detail", args=[foreign.pk]))
        self.assertEqual(response.status_code, 404)

    def test_renew_requires_an_expiry_date(self):
        document = Document.objects.create(owner=self.user, title="No Expiry")
        response = self.client.post(
            reverse("document_renew", args=[document.pk]),
            {"new_expiry_date": "2030-01-01", "notes": ""},
        )
        self.assertEqual(response.status_code, 302)  # bounced back to the detail page
        self.assertFalse(Renewal.objects.exists())



# ----------------------------------------------------------------------
# OCR feature tests (integrated into the Add Document page)
# ----------------------------------------------------------------------
# These tests use mocking so they do NOT require a local Tesseract
# installation. There is no separate OCR Scanner page any more: uploading a
# scan and extracting its details both happen on the Add Document page.
# ----------------------------------------------------------------------


@contextmanager
def mocked_ocr(text):
    """Patch the OCR engine with a fake one that returns ``text``."""
    from unittest.mock import MagicMock, patch

    from documents.services import ocr

    engine = MagicMock()
    engine.image_to_string.return_value = text
    with patch.object(ocr, "_pytesseract", engine), patch.object(
        ocr, "pytesseract", engine
    ):
        yield engine


@contextmanager
def mocked_ocr_failure():
    """Patch the OCR engine so it raises (as if Tesseract failed)."""
    from unittest.mock import MagicMock, patch

    from documents.services import ocr

    engine = MagicMock()
    engine.image_to_string.side_effect = RuntimeError("boom")
    with patch.object(ocr, "_pytesseract", engine), patch.object(
        ocr, "pytesseract", engine
    ):
        yield engine


def make_image_file(name="license.jpg", content_type="image/jpeg"):
    """Build a real (blank) image file so Pillow can open it in OCR tests."""
    buffer = BytesIO()
    Image.new("RGB", (60, 30), color="white").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=content_type)


class AddDocumentOcrUploadTests(TestCase):
    """Uploading a scan on the Add Document page runs OCR (mocked)."""

    def setUp(self):
        self.user = create_user("ocruser")
        self.client.login(username="ocruser", password="secret123")

    def test_add_document_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("document_create"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_add_document_page_offers_a_file_upload(self):
        response = self.client.get(reverse("document_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add Document")
        self.assertContains(response, "Upload Document")
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, 'type="file"')
        self.assertContains(response, "Extract Details")

    def test_extract_without_a_file_asks_for_one(self):
        response = self.client.post(
            reverse("document_create"), {"ocr_action": "extract"}, follow=True
        )
        self.assertContains(response, "Choose a document image first")
        self.assertEqual(Document.objects.count(), 0)

    def test_extract_rejects_an_unsupported_file_type(self):
        bad = SimpleUploadedFile(
            "report.pdf", b"not an image", content_type="application/pdf"
        )
        response = self.client.post(
            reverse("document_create"),
            {"ocr_action": "extract", "file": bad},
            follow=True,
        )
        self.assertContains(response, "Unsupported file type")
        self.assertEqual(Document.objects.count(), 0)

    def test_extract_rejects_an_oversized_file(self):
        big = SimpleUploadedFile(
            "big.jpg", b"X" * (6 * 1024 * 1024), content_type="image/jpeg"
        )
        response = self.client.post(
            reverse("document_create"),
            {"ocr_action": "extract", "file": big},
            follow=True,
        )
        self.assertContains(response, "File is too large")
        self.assertEqual(Document.objects.count(), 0)

    def test_extract_runs_ocr_without_creating_a_document(self):
        text = "DRIVING LICENCE\nDL No: KA0520110001234\nValid Till: 12/03/2027"
        with mocked_ocr(text):
            response = self.client.post(
                reverse("document_create"),
                {"ocr_action": "extract", "file": make_image_file()},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Document detected")
        self.assertContains(response, "Driving License")
        # The raw OCR text is never shown: OCR is a helper, not a feature.
        self.assertNotContains(response, "Extracted Text")
        # Extracting must NOT write anything to the database.
        self.assertEqual(Document.objects.count(), 0)

    def test_ocr_failure_still_allows_manual_entry(self):
        with mocked_ocr_failure():
            response = self.client.post(
                reverse("document_create"),
                {"ocr_action": "extract", "file": make_image_file("image.png")},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "extract information from this document")
        self.assertContains(response, "You can enter the details manually")
        self.assertEqual(Document.objects.count(), 0)

# ----------------------------------------------------------------------
# Stage 2: rule-based document recognition tests (no Tesseract needed)
# ----------------------------------------------------------------------


class DocumentRecognitionTests(TestCase):
    """Unit tests for recognize_document_type()."""

    def setUp(self):
        self.user = create_user("recognizer")
        self.client.login(username="recognizer", password="secret123")

    def test_driving_license_recognized(self):
        from documents.services.document_recognition import recognize_document_type

        result = recognize_document_type(
            "DRIVING LICENCE\nDL NO: KA0520110001234\nValid Upto: 12/03/2027"
        )
        self.assertEqual(result["document_type"], "driving_license")
        self.assertEqual(result["label"], "Driving License")

    def test_vehicle_insurance_recognized(self):
        from documents.services.document_recognition import recognize_document_type

        result = recognize_document_type(
            "MOTOR INSURANCE\nPolicy Number: 12345\nPremium: 5000\nIDV: 300000"
        )
        self.assertEqual(result["document_type"], "vehicle_insurance")

    def test_vehicle_registration_recognized(self):
        from documents.services.document_recognition import recognize_document_type

        result = recognize_document_type(
            "REGISTRATION CERTIFICATE\nRegistration No: KA01AB1234\n"
            "Chassis No: ABC123\nEngine No: XYZ456"
        )
        self.assertEqual(result["document_type"], "vehicle_registration")

    def test_passport_recognized(self):
        from documents.services.document_recognition import recognize_document_type

        result = recognize_document_type(
            "PASSPORT\nRepublic of India\nPassport No: N1234567\nNationality: Indian"
        )
        self.assertEqual(result["document_type"], "passport")

    def test_aadhaar_recognized(self):
        from documents.services.document_recognition import recognize_document_type

        result = recognize_document_type(
            "AADHAAR\nUnique Identification Authority of India\nUIDAI"
        )
        self.assertEqual(result["document_type"], "aadhaar")

    def test_pan_recognized(self):
        from documents.services.document_recognition import recognize_document_type

        result = recognize_document_type(
            "INCOME TAX DEPARTMENT\nPermanent Account Number\nABCDE1234F"
        )
        self.assertEqual(result["document_type"], "pan")

    def test_certificate_recognized(self):
        from documents.services.document_recognition import recognize_document_type

        result = recognize_document_type(
            "CERTIFICATE\nCertified that John has completed the course"
        )
        self.assertEqual(result["document_type"], "certificate")

    def test_empty_text_is_unknown(self):
        from documents.services.document_recognition import recognize_document_type

        result = recognize_document_type("   ")
        self.assertEqual(result["document_type"], "other")
        self.assertEqual(result["confidence"], "Low")

    def test_unrelated_text_is_unknown(self):
        from documents.services.document_recognition import recognize_document_type

        result = recognize_document_type("grocery list: milk, eggs, bread")
        self.assertEqual(result["document_type"], "other")

    def test_weak_common_words_do_not_classify(self):
        from documents.services.document_recognition import recognize_document_type

        result = recognize_document_type("Government of India\nsome random notice")
        self.assertEqual(result["document_type"], "other")

# ----------------------------------------------------------------------
# Stage 3A: driving license field extraction tests (no Tesseract needed)
# ----------------------------------------------------------------------

DL_SAMPLE = (
    "GOVERNMENT OF KARNATAKA\n"
    "DRIVING LICENCE\n"
    "Name: RAM DEEPAK\n"
    "S/O: Ramesh\n"
    "Date of Birth: 15/11/2006\n"
    "DL No: AP0120230012345\n"
    "Date of Issue: 15-01-2023\n"
    "Valid Till: 14.01.2043\n"
    "Class of Vehicle: MCWG, LMV\n"
)

INS_SAMPLE = (
    "MOTOR INSURANCE POLICY\n"
    "Insurance Company: ABC General Insurance Ltd\n"
    "Policy No: P-12345/2024/AB-6789\n"
    "Policy Holder: ANITA SHARMA\n"
    "Vehicle No: AP39AB1234\n"
    "Policy Period: 01/01/2025 - 31/12/2025\n"
    "IDV: Rs. 4,50,000\n"
    "Total Premium: Rs. 12,500\n"
)


RC_SAMPLE = (
    "GOVERNMENT OF INDIA\n"
    "MINISTRY OF ROAD TRANSPORT AND HIGHWAYS\n"
    "REGISTRATION CERTIFICATE\n"
    "Owner Name: RAJESH KUMAR\n"
    "Registration No: AP-39-AB-1234\n"
    "Date of Registration: 15/03/2024\n"
    "Vehicle Class: Motor Car\n"
    "Fuel Type: PETROL\n"
    "Maker's Name: MARUTI SUZUKI\n"
    "Model: BALENO\n"
    "Chassis No: MA1AB2CD34567890\n"
    "Engine No: EN71092345\n"
)


class DrivingLicenseExtractionTests(TestCase):
    """Unit tests for extract_driving_license_fields()."""

    def test_full_sample_extracts_all_fields(self):
        from documents.services.field_extraction import extract_driving_license_fields

        result = extract_driving_license_fields(DL_SAMPLE)
        self.assertEqual(result["document_type"], "Driving License")
        fields = result["fields"]
        self.assertEqual(fields["name"], "RAM DEEPAK")
        self.assertEqual(fields["license_number"], "AP0120230012345")
        self.assertEqual(fields["date_of_birth"], "15/11/2006")
        self.assertEqual(fields["issue_date"], "15/01/2023")
        self.assertEqual(fields["expiry_date"], "14/01/2043")
        self.assertEqual(fields["vehicle_class"], "MCWG, LMV")

    def test_license_number_with_spaces_and_hyphens(self):
        from documents.services.field_extraction import extract_driving_license_fields

        for raw in ("AP 0120230012345", "AP-0120230012345"):
            result = extract_driving_license_fields(
                "DRIVING LICENCE\nDL No: {}\n".format(raw)
            )
            self.assertEqual(result["fields"]["license_number"], "AP0120230012345")

    def test_random_number_without_label_is_ignored(self):
        from documents.services.field_extraction import extract_driving_license_fields

        result = extract_driving_license_fields(
            "DRIVING LICENCE\nName: RAM DEEPAK\n123456789012345\n"
        )
        self.assertEqual(result["fields"]["license_number"], "")

    def test_name_near_label(self):
        from documents.services.field_extraction import extract_driving_license_fields

        result = extract_driving_license_fields(
            "DRIVING LICENCE\nName of Holder\nSITA LAKSHMI\nS/O: Rao\n"
        )
        self.assertEqual(result["fields"]["name"], "SITA LAKSHMI")

    def test_header_is_never_returned_as_name(self):
        from documents.services.field_extraction import extract_driving_license_fields

        result = extract_driving_license_fields(
            "GOVERNMENT OF INDIA\nDRIVING LICENCE\nTRANSPORT DEPARTMENT\n"
        )
        self.assertEqual(result["fields"]["name"], "")

    def test_different_date_formats(self):
        from documents.services.field_extraction import extract_driving_license_fields

        result = extract_driving_license_fields(
            "DRIVING LICENCE\nDate of Birth: 15-11-2006\n"
            "Date of Issue: 15.01.2023\nValid Till: 14/01/2043\n"
        )
        fields = result["fields"]
        self.assertEqual(fields["date_of_birth"], "15/11/2006")
        self.assertEqual(fields["issue_date"], "15/01/2023")
        self.assertEqual(fields["expiry_date"], "14/01/2043")

    def test_two_digit_year_expiry(self):
        from documents.services.field_extraction import extract_driving_license_fields

        result = extract_driving_license_fields(
            "DRIVING LICENCE\nValid Till: 14/01/43\n"
        )
        self.assertEqual(result["fields"]["expiry_date"], "14/01/2043")

    def test_unlabeled_dates_are_ignored(self):
        from documents.services.field_extraction import extract_driving_license_fields

        result = extract_driving_license_fields(
            "DRIVING LICENCE\nName: RAM DEEPAK\n15/01/2023\n14/01/2043\n"
        )
        fields = result["fields"]
        self.assertEqual(fields["issue_date"], "")
        self.assertEqual(fields["expiry_date"], "")

    def test_missing_fields_are_empty(self):
        from documents.services.field_extraction import extract_driving_license_fields

        result = extract_driving_license_fields("DRIVING LICENCE\nDL No: KA01AB1234\n")
        fields = result["fields"]
        self.assertEqual(fields["name"], "")
        self.assertEqual(fields["date_of_birth"], "")
        self.assertEqual(fields["vehicle_class"], "")

    def test_invalid_dates_are_rejected(self):
        from documents.services.field_extraction import extract_driving_license_fields

        result = extract_driving_license_fields(
            "DRIVING LICENCE\nDate of Birth: 31/02/2025\nValid Till: 99/99/2025\n"
        )
        fields = result["fields"]
        self.assertEqual(fields["date_of_birth"], "")
        self.assertEqual(fields["expiry_date"], "")

    def test_expiry_before_issue_is_dropped(self):
        from documents.services.field_extraction import extract_driving_license_fields

        result = extract_driving_license_fields(
            "DRIVING LICENCE\nDate of Issue: 15/01/2023\nValid Till: 14/01/2020\n"
        )
        fields = result["fields"]
        self.assertEqual(fields["issue_date"], "15/01/2023")
        self.assertEqual(fields["expiry_date"], "")

    def test_non_driving_license_text_returns_none(self):
        from documents.services.field_extraction import extract_fields_for

        self.assertIsNone(extract_fields_for("PASSPORT text", "passport"))
        self.assertIsNone(extract_fields_for("random text", "other"))

# ----------------------------------------------------------------------
# Stage 3C: Vehicle Registration / RC field extraction tests
# ----------------------------------------------------------------------


class VehicleRegistrationExtractionTests(TestCase):
    """Unit tests for extract_vehicle_registration_fields()."""

    def test_full_sample_extracts_all_fields(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields(RC_SAMPLE)
        self.assertEqual(result["document_type"], "Vehicle Registration / RC")
        fields = result["fields"]
        self.assertEqual(fields["registration_number"], "AP39AB1234")
        self.assertEqual(fields["owner_name"], "RAJESH KUMAR")
        self.assertEqual(fields["registration_date"], "15/03/2024")
        self.assertEqual(fields["vehicle_class"], "Motor Car")
        self.assertEqual(fields["fuel_type"], "PETROL")
        self.assertEqual(fields["manufacturer"], "MARUTI SUZUKI")
        self.assertEqual(fields["model"], "BALENO")
        self.assertEqual(fields["chassis_number"], "MA1AB2CD34567890")
        self.assertEqual(fields["engine_number"], "EN71092345")

    def test_registration_number_extraction(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields(
            "REGISTRATION CERTIFICATE\nRegn No: KA01MN1234\n"
        )
        self.assertEqual(result["fields"]["registration_number"], "KA01MN1234")

    def test_registration_number_with_spaces_and_hyphens(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        for raw in ("AP 39 AB 1234", "AP-39-AB-1234"):
            result = extract_vehicle_registration_fields(
                "Registration No: {}\n".format(raw)
            )
            self.assertEqual(
                result["fields"]["registration_number"], "AP39AB1234"
            )

    def test_arbitrary_numbers_not_registration_number(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields(
            "REGISTRATION CERTIFICATE\n2024\n123456\n39\n"
        )
        self.assertEqual(result["fields"]["registration_number"], "")

    def test_owner_name_extraction(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields(
            "Vehicle Registration Certificate\nOwner Name: SITA LAKSHMI\n"
        )
        self.assertEqual(result["fields"]["owner_name"], "SITA LAKSHMI")

    def test_heading_is_never_returned_as_owner(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        # "Name of Manufacturer" is not an owner label, and heading-only
        # text must never be treated as a person's name.
        result = extract_vehicle_registration_fields(
            "GOVERNMENT OF INDIA\nMINISTRY OF ROAD TRANSPORT\n"
            "REGISTRATION CERTIFICATE\n"
            "Name of Manufacturer: MARUTI SUZUKI\n"
        )
        self.assertEqual(result["fields"]["owner_name"], "")
        self.assertEqual(result["fields"]["manufacturer"], "MARUTI SUZUKI")

        only_headers = extract_vehicle_registration_fields(
            "GOVERNMENT OF INDIA\nMINISTRY OF ROAD TRANSPORT\n"
            "REGISTRATION CERTIFICATE\n"
        )
        self.assertEqual(only_headers["fields"]["owner_name"], "")

    def test_registration_date_extraction(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        for label in (
            "Regn. Date: 15/03/2024",
            "Registration Dt: 15-03-2024",
        ):
            result = extract_vehicle_registration_fields(label + "\n")
            self.assertEqual(
                result["fields"]["registration_date"], "15/03/2024"
            )

    def test_multiple_date_formats(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        for raw_date in ("15-03-2024", "15.03.2024", "15/03/24"):
            result = extract_vehicle_registration_fields(
                "Registration Date: {}\n".format(raw_date)
            )
            self.assertEqual(
                result["fields"]["registration_date"], "15/03/2024"
            )

    def test_invalid_date_rejected(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields(
            "Registration Date: 31/02/2024\nRegn Date: 99/99/2025\n"
        )
        self.assertEqual(result["fields"]["registration_date"], "")

    def test_vehicle_class_extraction(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields(
            "Vehicle Class: MCWG, LMV\n"
        )
        self.assertEqual(result["fields"]["vehicle_class"], "MCWG, LMV")

        result = extract_vehicle_registration_fields(
            "Class of Vehicle: Motor Cycle\nNon Transport\n"
        )
        self.assertEqual(
            result["fields"]["vehicle_class"], "Motor Cycle, Non Transport"
        )

    def test_fuel_type_extraction(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields(
            "Fuel Type: DIESEL\nFuel: CNG\n"
        )
        self.assertEqual(result["fields"]["fuel_type"], "DIESEL")

        result = extract_vehicle_registration_fields("Fuel Type: Eletric\n")
        self.assertEqual(result["fields"]["fuel_type"], "ELECTRIC")

    def test_manufacturer_extraction(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields(
            "Manufacturer: HYUNDAI\n"
        )
        self.assertEqual(result["fields"]["manufacturer"], "HYUNDAI")

        # "Maker / Model: TATA NEXON" mixes two fields on one line:
        # extracting conservatively means we do not guess either one.
        result = extract_vehicle_registration_fields(
            "Maker / Model: TATA NEXON\n"
        )
        self.assertEqual(result["fields"]["manufacturer"], "")
        self.assertEqual(result["fields"]["model"], "")

    def test_model_extraction(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields("Model: i20\n")
        self.assertEqual(result["fields"]["model"], "i20")

        result = extract_vehicle_registration_fields("Vehicle Model: CRETA\n")
        self.assertEqual(result["fields"]["model"], "CRETA")

        # A bare number is not a model name.
        result = extract_vehicle_registration_fields("Model: 12345\n")
        self.assertEqual(result["fields"]["model"], "")

    def test_chassis_number_extraction(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields(
            "Chassis No: MA1AB2CD34567890\n"
        )
        self.assertEqual(
            result["fields"]["chassis_number"], "MA1AB2CD34567890"
        )

        # Without a chassis label the same string is never captured.
        result = extract_vehicle_registration_fields("MA1AB2CD34567890\n")
        self.assertEqual(result["fields"]["chassis_number"], "")

    def test_engine_number_extraction(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields("Engine No: EN71092345\n")
        self.assertEqual(result["fields"]["engine_number"], "EN71092345")

        # "Engine Power" is a specification, not the engine number.
        result = extract_vehicle_registration_fields("Engine Power: 120\n")
        self.assertEqual(result["fields"]["engine_number"], "")

    def test_missing_fields_return_empty(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields(
            "REGISTRATION CERTIFICATE\nFUEL: PETROL\n"
        )
        fields = result["fields"]
        self.assertEqual(fields["fuel_type"], "PETROL")
        for key in (
            "registration_number",
            "owner_name",
            "registration_date",
            "vehicle_class",
            "manufacturer",
            "model",
            "chassis_number",
            "engine_number",
        ):
            self.assertEqual(fields[key], "")

    def test_unrelated_text_no_false_fields(self):
        from documents.services.field_extraction import (
            extract_vehicle_registration_fields,
        )

        result = extract_vehicle_registration_fields(
            "grocery list: milk, eggs, bread\n"
        )
        for value in result["fields"].values():
            self.assertEqual(value, "")

    def test_rc_extractor_not_used_for_driving_license(self):
        from documents.services.field_extraction import extract_fields_for

        result = extract_fields_for(DL_SAMPLE, "driving_license")
        self.assertEqual(result["document_type"], "Driving License")
        self.assertNotIn("registration_number", result["fields"])

        routed_rc = extract_fields_for(RC_SAMPLE, "vehicle_registration")
        self.assertEqual(
            routed_rc["document_type"], "Vehicle Registration / RC"
        )

    def test_rc_extractor_not_used_for_vehicle_insurance(self):
        from documents.services.field_extraction import extract_fields_for

        result = extract_fields_for(INS_SAMPLE, "vehicle_insurance")
        self.assertEqual(result["document_type"], "Vehicle Insurance")
        self.assertNotIn("registration_number", result["fields"])

    def test_driving_license_extraction_still_works(self):
        from documents.services.field_extraction import (
            extract_driving_license_fields,
        )

        fields = extract_driving_license_fields(DL_SAMPLE)["fields"]
        self.assertEqual(fields["name"], "RAM DEEPAK")
        self.assertEqual(fields["license_number"], "AP0120230012345")
        self.assertEqual(fields["issue_date"], "15/01/2023")
        self.assertEqual(fields["expiry_date"], "14/01/2043")

    def test_vehicle_insurance_extraction_still_works(self):
        from documents.services.field_extraction import (
            extract_vehicle_insurance_fields,
        )

        fields = extract_vehicle_insurance_fields(INS_SAMPLE)["fields"]
        self.assertEqual(fields["policy_number"], "P-12345/2024/AB-6789")
        self.assertEqual(fields["policy_holder"], "ANITA SHARMA")
        self.assertEqual(fields["vehicle_number"], "AP39AB1234")
        self.assertEqual(fields["start_date"], "01/01/2025")
        self.assertEqual(fields["expiry_date"], "31/12/2025")



# ----------------------------------------------------------------------
# Add Document + OCR: review, verify and save the extracted document
# ----------------------------------------------------------------------
# The extracted values prefill the EXISTING Add Document form (mapped by


# ----------------------------------------------------------------------
# Primary identifier -> generic DocumentForm.document_number
# ----------------------------------------------------------------------
# "Document Number" is generic: it holds whatever the scanned document's
# PRIMARY unique identifier is (licence number, policy number, registration
# number...). The mapping layer reads it from PRIMARY_IDENTIFIER_KEYS so a
# future type only needs its own extractor + registry entry.
# ----------------------------------------------------------------------


class PrimaryIdentifierTests(TestCase):
    """The document-specific identifier must reach document_number."""

    def test_dl_licence_number_is_the_primary_identifier(self):
        fields = extract_driving_license_fields(DL_SAMPLE)["fields"]
        self.assertEqual(fields["license_number"], "AP0120230012345")
        values = build_document_values(
            "driving_license", build_ocr_initial("driving_license", fields)
        )
        self.assertEqual(values["document_number"], "AP0120230012345")

    def test_dl_labels_without_a_period_are_matched(self):
        fields = extract_driving_license_fields(
            "DRIVING LICENCE\nName: RAM DEEPAK\n"
            "DL No. KA0520110001234\nLicence Number: MH1220110045678\n"
        )["fields"]
        self.assertEqual(fields["license_number"], "KA0520110001234")

    def test_insurance_policy_number_is_the_primary_identifier(self):
        fields = extract_vehicle_insurance_fields(INS_SAMPLE)["fields"]
        self.assertEqual(fields["policy_number"], "P-12345/2024/AB-6789")
        values = build_document_values(
            "vehicle_insurance", build_ocr_initial("vehicle_insurance", fields)
        )
        self.assertEqual(values["document_number"], "P-12345/2024/AB-6789")

    def test_rc_registration_number_is_the_primary_identifier(self):
        fields = extract_vehicle_registration_fields(RC_SAMPLE)["fields"]
        self.assertEqual(fields["registration_number"], "AP39AB1234")
        values = build_document_values(
            "vehicle_registration",
            build_ocr_initial("vehicle_registration", fields),
        )
        self.assertEqual(values["document_number"], "AP39AB1234")

    def test_rc_bh_series_registration_number_is_matched(self):
        fields = extract_vehicle_registration_fields(
            "REGISTRATION CERTIFICATE\n"
            "Registration No: AP-24-BH-1234-AB\n"
        )["fields"]
        self.assertEqual(fields["registration_number"], "AP24BH1234AB")

    def test_missing_identifier_stays_empty_and_is_never_invented(self):
        fields = extract_driving_license_fields(
            "DRIVING LICENCE\nName: RAM DEEPAK\n"
            "Date of Birth: 15/11/2006\nValid Till: 14/01/2043\n"
        )["fields"]
        self.assertEqual(fields["license_number"], "")
        values = build_document_values(
            "driving_license", build_ocr_initial("driving_license", fields)
        )
        self.assertEqual(values["document_number"], "")

    def test_generic_identifier_uses_label_evidence(self):
        # "Document ID" is a strong identifier label.
        result = extract_primary_identifier(
            "Receipt\nDocument ID: A1B2C3D4E5\nPhone: 9876543210\n"
        )
        self.assertEqual(result["value"], "A1B2C3D4E5")
        self.assertEqual(result["evidence"], "strong_label")

    def test_generic_identifier_never_guesses_without_evidence(self):
        self.assertIsNone(
            extract_primary_identifier(
                "Bill\nPaid on: 01/02/2024\nAmount: Rs. 450000\n"
                "Contact: 9876543210\n"
                "Address: 12 MG Road Bangalore 560001\n"
            )
        )

    def test_registry_covers_supported_and_future_types(self):
        for doc_type in SUPPORTED_SAVE_TYPES:
            self.assertIn(doc_type, PRIMARY_IDENTIFIER_KEYS)
        self.assertEqual(
            (
                PRIMARY_IDENTIFIER_KEYS["driving_license"],
                PRIMARY_IDENTIFIER_KEYS["vehicle_insurance"],
                PRIMARY_IDENTIFIER_KEYS["vehicle_registration"],
                PRIMARY_IDENTIFIER_KEYS["passport"],
                PRIMARY_IDENTIFIER_KEYS["aadhaar"],
                PRIMARY_IDENTIFIER_KEYS["pan"],
                PRIMARY_IDENTIFIER_KEYS["certificate"],
            ),
            (
                "license_number",
                "policy_number",
                "registration_number",
                "passport_number",
                "aadhaar_number",
                "pan_number",
                "certificate_number",
            ),
        )


# ----------------------------------------------------------------------
# Add Document + OCR: review, verify and save the extracted document
# ----------------------------------------------------------------------
# The extracted values prefill the EXISTING Add Document form (mapped by
# services/ocr_save.py). The user reviews and edits them, and only the
# explicit "Add Document" click writes anything to the database.
# ----------------------------------------------------------------------


@override_settings(MEDIA_ROOT=MEDIA_ROOT_TEMP)
class AddDocumentOcrWorkflowTests(TestCase):
    """Upload -> OCR -> recognition -> extraction -> review -> Add Document."""

    def setUp(self):
        self.user = create_user("scanner")
        self.other = create_user("someoneelse")
        self.client.login(username="scanner", password="secret123")

    def tearDown(self):
        # Remove uploaded media + pending scans created by this test run so
        # no temporary files are left behind (the test DB is Django's own).
        shutil.rmtree(MEDIA_ROOT_TEMP, ignore_errors=True)
        os.makedirs(MEDIA_ROOT_TEMP, exist_ok=True)
        from . import views as documents_views

        shutil.rmtree(documents_views._ocr_pending_root(), ignore_errors=True)

    # -------------------------------------------------------- helpers
    def extract(self, sample, filename="scan.jpg"):
        """Upload a sample through the real "Extract Details" action."""
        with mocked_ocr(sample):
            return self.client.post(
                reverse("document_create"),
                {"ocr_action": "extract", "file": make_image_file(filename)},
                follow=True,
            )

    def form_initial(self, response):
        """The prefilled values of the Add Document form."""
        return response.context["form"].initial

    def post_data(self, response, **overrides):
        """Build the POST the browser sends from the prefilled form."""
        initial = self.form_initial(response)
        data = {}
        for name in (
            "title",
            "kind",
            "document_number",
            "issuing_authority",
            "issue_date",
            "expiry_date",
            "notes",
        ):
            value = initial.get(name)
            if isinstance(value, datetime.date):
                value = value.strftime("%Y-%m-%d")
            data[name] = value or ""
        data["ocr_assisted"] = "1"
        data.update(overrides)
        return data

    def pending_path(self):
        return self.client.session.get("ocr_file_path")

# -------------------------------------------- prefill + review UI
    def test_driving_license_ocr_prefills_the_add_document_form(self):
        response = self.extract(DL_SAMPLE, "license.jpg")
        self.assertEqual(response.status_code, 200)
        initial = self.form_initial(response)
        self.assertEqual(initial["title"], "Driving License - RAM DEEPAK")
        self.assertEqual(initial["kind"], "LIC")
        self.assertEqual(initial["document_number"], "AP0120230012345")
        self.assertEqual(initial["issue_date"], datetime.date(2023, 1, 15))
        self.assertEqual(initial["expiry_date"], datetime.date(2043, 1, 14))
        self.assertIn("Name: RAM DEEPAK", initial["notes"])
        self.assertIn("Date of Birth: 15/11/2006", initial["notes"])
        self.assertIn("Vehicle Class: MCWG, LMV", initial["notes"])
        # Detection + review alert are shown, and the form stays editable.
        self.assertContains(response, "Document detected")
        self.assertContains(response, "Driving License")
        self.assertContains(response, "Confidence")
        self.assertContains(response, "Review extracted information")
        self.assertContains(
            response,
            "Please verify and correct the fields before adding the document",
        )
        self.assertContains(response, 'name="ocr_assisted"')
        self.assertContains(response, "Add Document")
        # Uploading/extracting never writes to the database.
        self.assertEqual(Document.objects.count(), 0)

    def test_insurance_ocr_prefills_the_add_document_form(self):
        response = self.extract(INS_SAMPLE, "insurance.jpg")
        initial = self.form_initial(response)
        self.assertEqual(initial["title"], "Vehicle Insurance - ANITA SHARMA")
        self.assertEqual(initial["kind"], "OTHER")
        self.assertEqual(initial["document_number"], "P-12345/2024/AB-6789")
        self.assertEqual(initial["issue_date"], datetime.date(2025, 1, 1))
        self.assertEqual(initial["expiry_date"], datetime.date(2025, 12, 31))
        self.assertIn("Policy Holder: ANITA SHARMA", initial["notes"])
        self.assertIn("Vehicle Number: AP39AB1234", initial["notes"])
        self.assertIn("Insurer: ABC General Insurance Ltd", initial["notes"])
        self.assertIn("IDV: 450000", initial["notes"])
        self.assertIn("Premium: 12500", initial["notes"])
        self.assertContains(response, "Vehicle Insurance")
        self.assertContains(response, "Review extracted information")
        self.assertEqual(Document.objects.count(), 0)

    def test_rc_ocr_prefills_the_add_document_form(self):
        response = self.extract(RC_SAMPLE, "rc.jpg")
        initial = self.form_initial(response)
        self.assertEqual(initial["title"], "Vehicle RC - RAJESH KUMAR")
        self.assertEqual(initial["kind"], "OTHER")
        self.assertEqual(initial["document_number"], "AP39AB1234")
        self.assertEqual(initial["issue_date"], datetime.date(2024, 3, 15))
        # An RC never gets an invented expiry date.
        self.assertIsNone(initial["expiry_date"])
        self.assertIn("Owner Name: RAJESH KUMAR", initial["notes"])
        self.assertIn("Fuel Type: PETROL", initial["notes"])
        self.assertIn("Manufacturer: MARUTI SUZUKI", initial["notes"])
        self.assertIn("Model: BALENO", initial["notes"])
        self.assertIn("Chassis Number: MA1AB2CD34567890", initial["notes"])
        self.assertIn("Engine Number: EN71092345", initial["notes"])
        self.assertContains(response, "Review extracted information")
        self.assertEqual(Document.objects.count(), 0)

    def test_partial_extraction_prefills_what_was_found(self):
        # OCR only read the license number: everything else stays empty and
        # editable, and the user can complete it by hand.
        partial = "DRIVING LICENCE\nDL No: AP0120230012345\n"
        response = self.extract(partial, "partial.jpg")
        initial = self.form_initial(response)
        self.assertEqual(initial["document_number"], "AP0120230012345")
        self.assertIsNone(initial["expiry_date"])
        self.assertContains(response, "Review extracted information")
        self.assertEqual(Document.objects.count(), 0)

        data = self.post_data(
            response,
            title="Driving License (completed by hand)",
            expiry_date="2027-03-12",
        )
        response = self.client.post(reverse("document_create"), data, follow=True)
        document = Document.objects.get()
        self.assertEqual(document.document_number, "AP0120230012345")
        self.assertEqual(document.expiry_date, datetime.date(2027, 3, 12))
        self.assertContains(response, "Document added successfully.")

    def test_unsupported_document_type_shows_a_friendly_message(self):
        passport = (
            "PASSPORT\nRepublic of India\nPassport No: N1234567\nNationality: Indian"
        )
        response = self.extract(passport, "passport.jpg")
        self.assertEqual(Document.objects.count(), 0)
        self.assertContains(response, "extract information from this document")
        self.assertContains(response, "You can enter the details manually")
        # Nothing was prefilled, so no review alert and no duplicate guard.
        self.assertNotContains(response, "Review extracted information")
        self.assertNotContains(response, 'name="ocr_assisted"')

# -------------------------------------------------- add document / save
    def test_add_document_creates_exactly_one_document(self):
        response = self.extract(DL_SAMPLE)
        data = self.post_data(response)
        self.client.post(reverse("document_create"), data)
        self.assertEqual(Document.objects.count(), 1)
        # A double click / repeated POST must not create a second copy.
        response = self.client.post(reverse("document_create"), data, follow=True)
        self.assertEqual(Document.objects.count(), 1)
        self.assertContains(response, "This scanned document has already been added")

    def test_ocr_session_is_cleared_after_a_successful_save(self):
        response = self.extract(DL_SAMPLE)
        self.client.post(reverse("document_create"), self.post_data(response))
        session = self.client.session
        for key in (
            "ocr_result",
            "ocr_filename",
            "ocr_csrf_token",
            "ocr_document_type",
            "ocr_document_label",
            "ocr_confidence",
            "ocr_document_icon",
            "ocr_extracted_fields",
            "ocr_status",
            "ocr_file_path",
        ):
            self.assertNotIn(key, session)

    def test_saved_document_owner_is_request_user_and_cannot_be_spoofed(self):
        response = self.extract(DL_SAMPLE)
        data = self.post_data(
            response,
            owner=self.other.pk,
            owner_id=self.other.pk,
            user_id=self.other.pk,
        )
        self.client.post(reverse("document_create"), data)
        document = Document.objects.get()
        self.assertEqual(document.owner, self.user)  # always request.user
        self.assertNotEqual(document.owner, self.other)

    def test_user_can_correct_an_extracted_value_before_saving(self):
        response = self.extract(DL_SAMPLE)
        data = self.post_data(
            response,
            title="Driving License - RAM EDITED",
            notes="Name: RAM EDITED\nVehicle Class: LMV ONLY",
        )
        response = self.client.post(reverse("document_create"), data, follow=True)
        document = Document.objects.get()
        self.assertEqual(document.title, "Driving License - RAM EDITED")
        self.assertIn("Name: RAM EDITED", document.notes)
        self.assertNotIn("RAM DEEPAK", document.notes)
        self.assertContains(response, "Document added successfully.")
        self.assertContains(response, "Driving License - RAM EDITED")

    # -------------------------------------------------- field mapping
    def test_driving_license_fields_map_onto_the_document(self):
        response = self.extract(DL_SAMPLE)
        self.client.post(reverse("document_create"), self.post_data(response))
        document = Document.objects.get()
        self.assertEqual(document.title, "Driving License - RAM DEEPAK")
        self.assertEqual(document.kind, "LIC")
        self.assertEqual(document.document_number, "AP0120230012345")
        self.assertEqual(document.issue_date, datetime.date(2023, 1, 15))
        self.assertEqual(document.expiry_date, datetime.date(2043, 1, 14))
        self.assertIn("Name: RAM DEEPAK", document.notes)
        self.assertIn("Date of Birth: 15/11/2006", document.notes)
        self.assertIn("Vehicle Class: MCWG, LMV", document.notes)
        self.assertEqual(document.owner, self.user)

    def test_insurance_fields_map_onto_the_document(self):
        response = self.extract(INS_SAMPLE, "insurance.jpg")
        self.client.post(reverse("document_create"), self.post_data(response))
        document = Document.objects.get()
        self.assertEqual(document.title, "Vehicle Insurance - ANITA SHARMA")
        self.assertEqual(document.kind, "OTHER")
        self.assertEqual(document.document_number, "P-12345/2024/AB-6789")
        self.assertEqual(document.issue_date, datetime.date(2025, 1, 1))
        self.assertEqual(document.expiry_date, datetime.date(2025, 12, 31))
        self.assertIn("Policy Holder: ANITA SHARMA", document.notes)
        self.assertIn("Insurer: ABC General Insurance Ltd", document.notes)
        self.assertIn("IDV: 450000", document.notes)

    def test_rc_fields_map_onto_the_document_without_an_expiry(self):
        response = self.extract(RC_SAMPLE, "rc.jpg")
        self.client.post(reverse("document_create"), self.post_data(response))
        document = Document.objects.get()
        self.assertEqual(document.title, "Vehicle RC - RAJESH KUMAR")
        self.assertEqual(document.kind, "OTHER")
        self.assertEqual(document.document_number, "AP39AB1234")
        self.assertEqual(document.issue_date, datetime.date(2024, 3, 15))
        self.assertIsNone(document.expiry_date)  # never invented for an RC
        self.assertIn("Owner Name: RAJESH KUMAR", document.notes)
        self.assertIn("Model: BALENO", document.notes)
        self.assertEqual(document.status(), "unknown")

    def test_empty_optional_fields_are_handled(self):
        # OCR found nothing but a license number; everything else is empty.
        partial = "DRIVING LICENCE\nDL No: AP0120230012345\n"
        response = self.extract(partial, "partial.jpg")
        data = self.post_data(response)
        data.update({"title": "Driving License", "notes": "", "expiry_date": ""})
        response = self.client.post(reverse("document_create"), data, follow=True)
        document = Document.objects.get()
        self.assertEqual(document.document_number, "AP0120230012345")
        self.assertIsNone(document.issue_date)
        self.assertIsNone(document.expiry_date)
        self.assertEqual(document.notes, "")
        self.assertEqual(document.status(), "unknown")
        self.assertContains(response, "Document added successfully.")

# -------------------------------------------------- validation
    def test_invalid_date_is_rejected_and_nothing_is_saved(self):
        response = self.extract(DL_SAMPLE)
        data = self.post_data(response, issue_date="not-a-date")
        response = self.client.post(reverse("document_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid date.")
        self.assertEqual(Document.objects.count(), 0)
        # The scan is still waiting, so the user can fix the mistake.
        self.assertEqual(self.client.session.get("ocr_status"), "review")
        self.assertIn("ocr_extracted_fields", self.client.session)

    def test_expiry_before_issue_is_rejected(self):
        response = self.extract(DL_SAMPLE)
        data = self.post_data(
            response, issue_date="2023-01-15", expiry_date="2020-01-14"
        )
        response = self.client.post(reverse("document_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Expiry date must be on or after the issue date."
        )
        self.assertEqual(Document.objects.count(), 0)

    def test_insurance_expiry_before_start_is_rejected(self):
        response = self.extract(INS_SAMPLE, "insurance.jpg")
        data = self.post_data(
            response, issue_date="2025-01-01", expiry_date="2024-12-31"
        )
        response = self.client.post(reverse("document_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Expiry date must be on or after the issue date."
        )
        self.assertEqual(Document.objects.count(), 0)

    # -------------------------------------------------- files
    def test_the_uploaded_scan_becomes_the_document_file(self):
        response = self.extract(DL_SAMPLE, "my_license.jpg")
        scan_path = self.pending_path()
        self.assertTrue(scan_path and os.path.exists(scan_path))
        self.client.post(reverse("document_create"), self.post_data(response))
        document = Document.objects.get()
        self.assertTrue(document.file)
        self.assertTrue(document.file.name.endswith("my_license.jpg"))
        self.assertGreater(document.file.size, 0)
        # The temporary copy is gone - there is no duplicate file.
        self.assertFalse(os.path.exists(scan_path))

    def test_start_over_discards_the_scan_and_its_temporary_file(self):
        self.extract(DL_SAMPLE)
        scan_path = self.pending_path()
        self.assertTrue(scan_path and os.path.exists(scan_path))
        response = self.client.post(
            reverse("document_create"), {"ocr_action": "reset"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(os.path.exists(scan_path))
        self.assertNotIn("ocr_extracted_fields", self.client.session)
        self.assertNotContains(response, "Review extracted information")
        self.assertEqual(Document.objects.count(), 0)

    def test_ocr_failure_still_allows_a_manual_document_with_the_scan(self):
        with mocked_ocr_failure():
            response = self.client.post(
                reverse("document_create"),
                {"ocr_action": "extract", "file": make_image_file("broken.jpg")},
                follow=True,
            )
        self.assertContains(response, "extract information from this document")
        self.assertEqual(Document.objects.count(), 0)
        # The user types the details by hand; the scan is still attached.
        self.client.post(
            reverse("document_create"),
            {"title": "Manual License", "kind": "LIC", "expiry_date": "2030-01-01"},
        )
        document = Document.objects.get()
        self.assertEqual(document.owner, self.user)
        self.assertTrue(document.file)

    # -------------------------------------------------- integration
    def test_saved_document_appears_in_documents_dashboard_and_detail(self):
        response = self.extract(DL_SAMPLE)
        self.client.post(reverse("document_create"), self.post_data(response))
        document = Document.objects.get()
        self.assertContains(self.client.get(reverse("document_list")), document.title)
        self.assertContains(self.client.get(reverse("dashboard")), document.title)
        detail = self.client.get(reverse("document_detail", args=[document.pk]))
        self.assertContains(detail, document.title)
        self.assertContains(detail, "AP0120230012345")
        self.assertContains(detail, "MCWG, LMV")
        # It immediately takes part in expiry tracking.
        self.assertEqual(document.status(), "valid")

    def test_manual_add_without_any_upload_still_works(self):
        response = self.client.post(
            reverse("document_create"),
            {"title": "Rental Agreement", "kind": "CERT", "expiry_date": "2030-01-01"},
            follow=True,
        )
        document = Document.objects.get()
        self.assertEqual(document.owner, self.user)
        self.assertFalse(document.file)
        self.assertContains(response, "Document added successfully.")
        self.assertNotContains(response, "Review extracted information")

    def test_end_to_end_mocked_flow_from_upload_to_detail_page(self):
        """OCR -> recognition -> extraction -> prefilled form -> edit -> add."""
        response = self.extract(DL_SAMPLE, "license.jpg")
        self.assertContains(response, "Driving License")
        self.assertContains(response, "Review extracted information")
        self.assertEqual(Document.objects.count(), 0)

        data = self.post_data(response, title="Driving License - RAM DEEPAK EDITED")
        response = self.client.post(reverse("document_create"), data, follow=True)

        document = Document.objects.get()
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(document.owner, self.user)
        self.assertEqual(document.title, "Driving License - RAM DEEPAK EDITED")
        self.assertEqual(document.kind, "LIC")
        self.assertContains(response, "Document added successfully.")
        self.assertContains(response, "Driving License - RAM DEEPAK EDITED")
        self.assertNotIn("ocr_extracted_fields", self.client.session)


class OcrUiRemovalTests(TestCase):
    """The standalone OCR Scanner page is gone: Add Document is the only way."""

    def setUp(self):
        self.user = create_user("navigator")
        self.client.login(username="navigator", password="secret123")

    def test_sidebar_has_no_ocr_scanner_link(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Add Document")
        self.assertNotContains(response, "OCR Scanner")
        self.assertNotContains(response, "ocr_scan")

    def test_the_old_scan_routes_are_removed(self):
        for path in (
            "/documents/scan/",
            "/documents/scan-request/",
            "/documents/scan/save/",
        ):
            self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.client.post(path).status_code, 404)

    def test_add_document_is_the_ocr_entry_point(self):
        response = self.client.get(reverse("document_create"))
        self.assertContains(response, "Upload Document")
        self.assertContains(response, "Extract Details")
        self.assertContains(response, "Add Document")


class OcrUploadValidationTests(TestCase):
    """The upload rules shared by Add Document and OcrScanForm."""

    def test_supported_extensions_are_accepted(self):
        from .forms import validate_ocr_upload

        for name in ("a.jpg", "b.jpeg", "c.png", "d.webp", "E.JPG"):
            upload = make_image_file(name)
            self.assertIs(validate_ocr_upload(upload), upload)

    def test_unsupported_extension_is_rejected(self):
        from django.core.exceptions import ValidationError

        from .forms import validate_ocr_upload

        with self.assertRaises(ValidationError) as caught:
            validate_ocr_upload(SimpleUploadedFile("a.pdf", b"x"))
        self.assertIn("Unsupported file type", str(caught.exception))

    def test_oversized_file_is_rejected(self):
        from django.core.exceptions import ValidationError

        from .forms import validate_ocr_upload

        too_big = SimpleUploadedFile("a.jpg", b"x" * (5 * 1024 * 1024 + 1))
        with self.assertRaises(ValidationError) as caught:
            validate_ocr_upload(too_big)
        self.assertIn("File is too large", str(caught.exception))

    def test_no_file_is_returned_unchanged(self):
        from .forms import validate_ocr_upload

        self.assertIsNone(validate_ocr_upload(None))

    def test_ocr_scan_form_uses_the_same_rules(self):
        from .forms import OcrScanForm

        form = OcrScanForm({}, {"scan_file": SimpleUploadedFile("a.pdf", b"x")})
        self.assertFalse(form.is_valid())
        self.assertIn("Unsupported file type", form.errors["scan_file"][0])


# ----------------------------------------------------------------------
# Feature: user-created document groups (folders for related documents)
# ----------------------------------------------------------------------
# A group ("Car Documents") is a user organisation choice, never an OCR
# result. Every group view is owner-scoped with
# get_object_or_404(DocumentGroup, pk=..., owner=request.user), the document
# form only offers/accepts the logged-in user's own groups, and a document
# may always stay ungrouped. Deleting a group NEVER deletes documents:
# Document.group uses on_delete=SET_NULL, so its documents become ungrouped.
# ----------------------------------------------------------------------


def make_group(user, name="Car Documents"):
    """Helper that builds and saves a DocumentGroup for ``user``."""
    return DocumentGroup.objects.create(owner=user, name=name)


class DocumentGroupModelTests(TestCase):
    """The group model: ownership, duplicate names and delete behaviour."""

    def setUp(self):
        self.user = create_user("alice")
        self.other = create_user("bob")

    def test_group_belongs_to_the_user_who_created_it(self):
        group = make_group(self.user)
        self.assertEqual(group.owner, self.user)
        self.assertIn(group, self.user.document_groups.all())
        self.assertNotIn(group, self.other.document_groups.all())

    def test_created_and_updated_timestamps_are_set(self):
        group = make_group(self.user)
        self.assertIsNotNone(group.created_at)
        self.assertIsNotNone(group.updated_at)

    def test_str_is_the_group_name(self):
        group = make_group(self.user, "Bike Documents")
        self.assertEqual(str(group), "Bike Documents")

    def test_same_user_cannot_create_duplicate_group_names(self):
        make_group(self.user, "Car Documents")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_group(self.user, "Car Documents")

    def test_different_users_can_use_the_same_group_name(self):
        mine = make_group(self.user, "Car Documents")
        theirs = make_group(self.other, "Car Documents")
        self.assertEqual(mine.name, theirs.name)
        self.assertEqual(
            DocumentGroup.objects.filter(name="Car Documents").count(), 2
        )

    def test_document_can_be_created_without_a_group(self):
        document = make_document(self.user, title="Ungrouped Doc")
        self.assertIsNone(document.group)

    def test_document_can_be_assigned_to_a_group(self):
        group = make_group(self.user)
        document = make_document(self.user, title="Car RC", group=group)
        self.assertEqual(document.group, group)
        self.assertIn(document, group.documents.all())

    def test_deleting_a_group_keeps_its_documents(self):
        group = make_group(self.user)
        document = make_document(self.user, title="Car RC", group=group)
        group.delete()
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())

    def test_deleting_a_group_ungroups_its_documents(self):
        group = make_group(self.user)
        document = make_document(self.user, title="Car RC", group=group)
        group.delete()
        document.refresh_from_db()
        self.assertIsNone(document.group)

    def test_deleting_a_group_leaves_other_documents_untouched(self):
        group = make_group(self.user, "Car Documents")
        other_group = make_group(self.user, "Bike Documents")
        inside = make_document(self.user, title="Car RC", group=group)
        elsewhere = make_document(self.user, title="Bike RC", group=other_group)
        group.delete()
        inside.refresh_from_db()
        elsewhere.refresh_from_db()
        self.assertIsNone(inside.group)
        self.assertEqual(elsewhere.group, other_group)

    def test_deleting_a_document_does_not_delete_its_group(self):
        group = make_group(self.user)
        document = make_document(self.user, title="Car RC", group=group)
        document.delete()
        self.assertTrue(DocumentGroup.objects.filter(pk=group.pk).exists())


class DocumentGroupViewTests(TestCase):
    """Create, rename and delete groups through the normal UI."""

    def setUp(self):
        self.user = create_user("alice")
        self.other = create_user("bob")
        self.client.login(username="alice", password="secret123")

    def test_group_pages_require_login(self):
        group = make_group(self.user)
        self.client.logout()
        for url in (
            reverse("group_list"),
            reverse("group_create"),
            reverse("group_detail", args=[group.pk]),
            reverse("group_update", args=[group.pk]),
            reverse("group_delete", args=[group.pk]),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/accounts/login/", response.url)

    def test_group_list_page_renders(self):
        response = self.client.get(reverse("group_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New Group")

    def test_create_group(self):
        response = self.client.post(
            reverse("group_create"), {"name": "Car Documents"}, follow=True
        )
        self.assertContains(response, "Group “Car Documents” created.")
        group = DocumentGroup.objects.get()
        self.assertEqual(group.name, "Car Documents")
        self.assertEqual(group.owner, self.user)

    def test_create_group_owner_cannot_be_spoofed(self):
        self.client.post(
            reverse("group_create"),
            {
                "name": "Personal Documents",
                "owner": self.other.pk,
                "owner_id": self.other.pk,
            },
        )
        group = DocumentGroup.objects.get()
        self.assertEqual(group.owner, self.user)
        self.assertNotEqual(group.owner, self.other)

    def test_blank_group_name_is_rejected(self):
        # CharField(required=True) rejects a whitespace-only name for us, and
        # clean_name() keeps its own guard as a backstop.
        response = self.client.post(reverse("group_create"), {"name": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required.")
        self.assertEqual(DocumentGroup.objects.count(), 0)

    def test_duplicate_group_name_is_rejected(self):
        make_group(self.user, "Car Documents")
        response = self.client.post(reverse("group_create"), {"name": "Car Documents"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You already have a group called")
        self.assertEqual(DocumentGroup.objects.filter(owner=self.user).count(), 1)

    def test_duplicate_group_name_check_ignores_letter_case(self):
        make_group(self.user, "Car Documents")
        response = self.client.post(reverse("group_create"), {"name": "car documents"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You already have a group called")
        self.assertEqual(DocumentGroup.objects.filter(owner=self.user).count(), 1)

    def test_another_user_may_reuse_the_same_group_name(self):
        make_group(self.other, "Car Documents")
        response = self.client.post(
            reverse("group_create"), {"name": "Car Documents"}, follow=True
        )
        self.assertContains(response, "Group “Car Documents” created.")
        self.assertEqual(DocumentGroup.objects.filter(owner=self.user).count(), 1)

    def test_rename_group(self):
        group = make_group(self.user, "Car Documents")
        response = self.client.post(
            reverse("group_update", args=[group.pk]),
            {"name": "Vehicle Documents"},
            follow=True,
        )
        self.assertContains(response, "Group renamed.")
        group.refresh_from_db()
        self.assertEqual(group.name, "Vehicle Documents")

    def test_rename_to_an_existing_group_name_is_rejected(self):
        make_group(self.user, "Car Documents")
        group = make_group(self.user, "Bike Documents")
        response = self.client.post(
            reverse("group_update", args=[group.pk]), {"name": "Car Documents"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You already have a group called")
        group.refresh_from_db()
        self.assertEqual(group.name, "Bike Documents")

    def test_renaming_a_group_keeps_its_documents(self):
        group = make_group(self.user, "Car Documents")
        document = make_document(self.user, title="Car RC", group=group)
        self.client.post(
            reverse("group_update", args=[group.pk]), {"name": "Vehicle Documents"}
        )
        document.refresh_from_db()
        self.assertEqual(document.group, group)

    def test_group_list_shows_every_group_with_its_document_count(self):
        car = make_group(self.user, "Car Documents")
        bike = make_group(self.user, "Bike Documents")
        make_document(self.user, title="Car RC", group=car)
        make_document(self.user, title="Car Insurance", group=car)
        make_document(self.user, title="Bike RC", group=bike)
        response = self.client.get(reverse("group_list"))
        self.assertContains(response, "Car Documents")
        self.assertContains(response, "Bike Documents")
        self.assertContains(response, "2 documents")
        self.assertContains(response, "1 document")

    def test_delete_group_page_asks_for_confirmation(self):
        group = make_group(self.user, "Car Documents")
        response = self.client.get(reverse("group_delete", args=[group.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delete the group")
        # The user is told the documents themselves are kept.
        self.assertContains(response, "not</strong> deleted")

    def test_deleting_a_group_keeps_and_ungroups_its_documents(self):
        group = make_group(self.user, "Car Documents")
        group_pk = group.pk
        first = make_document(self.user, title="Car RC", group=group)
        second = make_document(self.user, title="Car Insurance", group=group)
        response = self.client.post(
            reverse("group_delete", args=[group_pk]), follow=True
        )
        self.assertContains(response, "deleted")
        self.assertFalse(DocumentGroup.objects.filter(pk=group_pk).exists())
        # The documents themselves are still there, just ungrouped.
        self.assertEqual(Document.objects.count(), 2)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.group)
        self.assertIsNone(second.group)
        listing = self.client.get(reverse("document_list"))
        self.assertContains(listing, "Car RC")
        self.assertContains(listing, "Car Insurance")

    def test_deleting_a_group_only_removes_that_group(self):
        car = make_group(self.user, "Car Documents")
        bike = make_group(self.user, "Bike Documents")
        kept = make_document(self.user, title="Bike RC", group=bike)
        self.client.post(reverse("group_delete", args=[car.pk]))
        self.assertTrue(DocumentGroup.objects.filter(pk=bike.pk).exists())
        kept.refresh_from_db()
        self.assertEqual(kept.group, bike)


class DocumentGroupSecurityTests(TestCase):
    """A user must never reach another user's group, or group their documents."""

    def setUp(self):
        self.user = create_user("alice")
        self.other = create_user("bob")
        self.client.login(username="alice", password="secret123")
        self.mine = make_group(self.user, "Car Documents")
        self.theirs = make_group(self.other, "Bob Private Group")
        self.their_document = make_document(
            self.other, title="Bob Secret RC", group=self.theirs
        )

    def test_user_cannot_view_another_users_group(self):
        response = self.client.get(reverse("group_detail", args=[self.theirs.pk]))
        self.assertEqual(response.status_code, 404)

    def test_user_cannot_rename_another_users_group(self):
        url = reverse("group_update", args=[self.theirs.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"name": "Hijacked"}).status_code, 404)
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.name, "Bob Private Group")

    def test_user_cannot_delete_another_users_group(self):
        url = reverse("group_delete", args=[self.theirs.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertTrue(DocumentGroup.objects.filter(pk=self.theirs.pk).exists())

    def test_user_cannot_assign_another_users_group_to_a_new_document(self):
        response = self.client.post(
            reverse("document_create"),
            {
                "title": "Sneaky Doc",
                "kind": "OTHER",
                "group": self.theirs.pk,
                "expiry_date": "2030-01-01",
            },
        )
        self.assertEqual(response.status_code, 200)  # form shown again
        self.assertContains(response, "not one of the available choices")
        self.assertFalse(Document.objects.filter(title="Sneaky Doc").exists())
        self.assertFalse(
            Document.objects.filter(owner=self.user, group=self.theirs).exists()
        )
        self.assertEqual(self.theirs.documents.count(), 1)  # untouched

    def test_user_cannot_move_their_document_into_another_users_group(self):
        document = make_document(self.user, title="My RC")
        response = self.client.post(
            reverse("document_update", args=[document.pk]),
            {
                "title": "My RC",
                "kind": "OTHER",
                "group": self.theirs.pk,
                "expiry_date": "2030-01-01",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not one of the available choices")
        document.refresh_from_db()
        self.assertIsNone(document.group)

    def test_group_dropdown_never_offers_another_users_groups(self):
        add_page = self.client.get(reverse("document_create"))
        self.assertContains(add_page, "Car Documents")
        self.assertNotContains(add_page, "Bob Private Group")

        document = make_document(self.user, title="My RC")
        edit_page = self.client.get(reverse("document_update", args=[document.pk]))
        self.assertNotContains(edit_page, "Bob Private Group")

    def test_group_list_never_shows_another_users_groups(self):
        response = self.client.get(reverse("group_list"))
        self.assertContains(response, "Car Documents")
        self.assertNotContains(response, "Bob Private Group")

    def test_group_detail_never_shows_another_users_documents(self):
        # Even if a foreign document were attached to my group at the
        # database level, the group page stays own-data only.
        make_document(self.other, title="Bob Secret RC 2", group=self.mine)
        make_document(self.user, title="My Car RC", group=self.mine)
        response = self.client.get(reverse("group_detail", args=[self.mine.pk]))
        self.assertContains(response, "My Car RC")
        self.assertNotContains(response, "Bob Secret RC")

    def test_group_counter_never_counts_another_users_documents(self):
        make_document(self.other, title="Bob Secret RC 3", group=self.mine)
        response = self.client.get(reverse("group_list"))
        self.assertContains(response, "0 documents")
        self.assertNotContains(response, "1 document")


class DocumentGroupFilterTests(TestCase):
    """The Documents page and a group page show exactly the right documents."""

    def setUp(self):
        self.user = create_user("alice")
        self.other = create_user("bob")
        self.client.login(username="alice", password="secret123")
        self.car = make_group(self.user, "Car Documents")
        self.bike = make_group(self.user, "Bike Documents")
        self.car_rc = make_document(
            self.user, title="Car RC", kind="OTHER", group=self.car
        )
        self.car_insurance = make_document(
            self.user, title="Car Insurance", kind="OTHER", group=self.car
        )
        self.bike_rc = make_document(
            self.user, title="Bike RC", kind="OTHER", group=self.bike
        )
        self.ungrouped = make_document(self.user, title="Aadhaar", kind="ID")

    def test_group_page_shows_only_that_groups_documents(self):
        response = self.client.get(reverse("group_detail", args=[self.car.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Car RC")
        self.assertContains(response, "Car Insurance")
        self.assertNotContains(response, "Bike RC")
        self.assertNotContains(response, "Aadhaar")

    def test_group_page_shows_the_group_name_and_a_way_back(self):
        response = self.client.get(reverse("group_detail", args=[self.car.pk]))
        self.assertContains(response, "Car Documents")
        # The group page is the Documents page in group context, so its back
        # link returns to the unfiltered Documents page.
        self.assertContains(response, "Back to Documents")

    def test_group_page_keeps_the_same_document_card_ui(self):
        response = self.client.get(reverse("group_detail", args=[self.car.pk]))
        self.assertContains(response, "document-card")
        self.assertContains(
            response, reverse("document_detail", args=[self.car_rc.pk])
        )
        self.assertContains(response, "status-pill")

    def test_all_documents_page_still_shows_every_document_once(self):
        response = self.client.get(reverse("document_list"))
        for title in ("Car RC", "Car Insurance", "Bike RC", "Aadhaar"):
            self.assertContains(response, title)
        # Grouping never duplicates document records.
        self.assertEqual(Document.objects.count(), 4)

    def test_group_page_keeps_the_search_filter(self):
        response = self.client.get(
            reverse("group_detail", args=[self.car.pk]), {"q": "insurance"}
        )
        self.assertContains(response, "Car Insurance")
        self.assertNotContains(response, "Car RC")

    def test_group_page_keeps_the_kind_filter(self):
        response = self.client.get(
            reverse("group_detail", args=[self.car.pk]), {"kind": "ID"}
        )
        self.assertNotContains(response, "Car RC")
        self.assertContains(response, "No documents match your search or filters.")

    def test_group_page_keeps_the_status_filter(self):
        make_document(
            self.user,
            title="Car PUC",
            kind="OTHER",
            group=self.car,
            expiry_days=400,  # still valid, so an "expired" filter hides it
        )
        response = self.client.get(
            reverse("group_detail", args=[self.car.pk]), {"status": "expired"}
        )
        self.assertNotContains(response, "Car PUC")

    def test_empty_group_shows_an_empty_message(self):
        empty = make_group(self.user, "Personal Documents")
        response = self.client.get(reverse("group_detail", args=[empty.pk]))
        self.assertContains(response, "This group is empty")

    def test_documents_page_lists_the_groups_with_counts(self):
        response = self.client.get(reverse("document_list"))
        self.assertContains(response, "Car Documents")
        self.assertContains(response, "Bike Documents")
        # 2 documents in "Car Documents", 1 in "Bike Documents".
        self.assertContains(
            response, '<span class="group-count">2</span>', html=False
        )
        self.assertContains(
            response, '<span class="group-count">1</span>', html=False
        )

    def test_group_chip_links_to_the_group_page(self):
        response = self.client.get(reverse("document_list"))
        self.assertContains(response, reverse("group_detail", args=[self.car.pk]))


class AddDocumentGroupTests(TestCase):
    """Manual Add Document and Edit Document: choosing / changing a group."""

    def setUp(self):
        self.user = create_user("alice")
        self.other = create_user("bob")
        self.client.login(username="alice", password="secret123")
        self.car = make_group(self.user, "Car Documents")
        self.bike = make_group(self.user, "Bike Documents")
        make_group(self.other, "Bob Private Group")

    def add_data(self, **overrides):
        data = {
            "title": "Car RC",
            "kind": "OTHER",
            "group": "",
            "document_number": "KA01AB1234",
            "issuing_authority": "RTO Bengaluru",
            "issue_date": "2020-01-01",
            "expiry_date": "2030-01-01",
            "notes": "",
        }
        data.update(overrides)
        return data

    def edit_data(self, document, **overrides):
        data = {
            "title": document.title,
            "kind": document.kind,
            "group": document.group_id or "",
            "document_number": document.document_number,
            "issuing_authority": document.issuing_authority,
            "issue_date": document.issue_date or "",
            "expiry_date": document.expiry_date or "",
            "notes": document.notes,
        }
        data.update(overrides)
        return data

    def test_add_document_page_offers_the_group_field(self):
        response = self.client.get(reverse("document_create"))
        self.assertContains(response, '<select name="group"')
        self.assertContains(response, "No Group")
        self.assertContains(response, "Car Documents")
        self.assertContains(response, "Bike Documents")
        # Another user's groups are never offered.
        self.assertNotContains(response, "Bob Private Group")

    def test_manual_add_document_with_a_group(self):
        response = self.client.post(
            reverse("document_create"), self.add_data(group=self.car.pk), follow=True
        )
        self.assertContains(response, "Document added successfully.")
        document = Document.objects.get()
        self.assertEqual(document.owner, self.user)
        self.assertEqual(document.group, self.car)
        self.assertEqual(self.car.documents.count(), 1)

    def test_manual_add_document_without_a_group(self):
        self.client.post(reverse("document_create"), self.add_data())
        document = Document.objects.get()
        self.assertIsNone(document.group)

    def test_edit_page_shows_the_current_group_selected(self):
        document = make_document(self.user, title="Car RC", group=self.car)
        response = self.client.get(reverse("document_update", args=[document.pk]))
        self.assertContains(
            response, '<option value="%d" selected>' % self.car.pk, html=False
        )
        self.assertContains(response, "No Group")
        self.assertNotContains(response, "Bob Private Group")

    def test_edit_document_can_move_it_to_another_group(self):
        document = make_document(self.user, title="Car RC", group=self.car)
        response = self.client.post(
            reverse("document_update", args=[document.pk]),
            self.edit_data(document, group=self.bike.pk),
            follow=True,
        )
        self.assertContains(response, "Document updated successfully.")
        document.refresh_from_db()
        self.assertEqual(document.group, self.bike)
        self.assertEqual(self.bike.documents.count(), 1)
        self.assertEqual(self.car.documents.count(), 0)

    def test_edit_document_can_remove_it_from_its_group(self):
        document = make_document(self.user, title="Car RC", group=self.car)
        self.client.post(
            reverse("document_update", args=[document.pk]),
            self.edit_data(document, group=""),  # the "No Group" option
            follow=True,
        )
        document.refresh_from_db()
        self.assertIsNone(document.group)
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())

    def test_edit_document_keeps_its_group_when_the_form_is_unchanged(self):
        document = make_document(
            self.user, title="Car RC", group=self.car, expiry_days=10
        )
        expiry = document.expiry_date
        self.client.post(
            reverse("document_update", args=[document.pk]), self.edit_data(document)
        )
        document.refresh_from_db()
        self.assertEqual(document.group, self.car)
        self.assertEqual(document.title, "Car RC")
        self.assertEqual(document.expiry_date, expiry)

    def test_document_detail_page_shows_the_group_and_links_to_it(self):
        document = make_document(self.user, title="Car RC", group=self.car)
        response = self.client.get(reverse("document_detail", args=[document.pk]))
        self.assertContains(response, "Car Documents")
        self.assertContains(response, reverse("group_detail", args=[self.car.pk]))

    def test_ungrouped_document_detail_shows_no_group(self):
        document = make_document(self.user, title="Loose Doc")
        response = self.client.get(reverse("document_detail", args=[document.pk]))
        self.assertContains(response, "Group")
        self.assertNotContains(response, "Car Documents")


@override_settings(MEDIA_ROOT=MEDIA_ROOT_TEMP)
class OcrDocumentGroupTests(TestCase):
    """The OCR-assisted Add Document flow lets the USER choose the group.

    OCR itself is mocked (no Tesseract needed). The group is a user
    organisation choice, never an OCR result: the dropdown starts on
    "No Group" and the saved document only lands in a group the user
    explicitly picked while reviewing the extracted fields.
    """

    def setUp(self):
        self.user = create_user("ocrgroupuser")
        self.other = create_user("ocrgroupother")
        self.car = make_group(self.user, "Car Documents")
        make_group(self.other, "Bob Private Group")
        self.client.login(username="ocrgroupuser", password="secret123")

    def tearDown(self):
        # Remove uploaded media + pending scans left behind by this test run.
        shutil.rmtree(MEDIA_ROOT_TEMP, ignore_errors=True)
        os.makedirs(MEDIA_ROOT_TEMP, exist_ok=True)
        from . import views as documents_views

        shutil.rmtree(documents_views._ocr_pending_root(), ignore_errors=True)

    def extract(self, sample=DL_SAMPLE, filename="license.jpg"):
        """Upload a scan through the real "Extract Details" action."""
        with mocked_ocr(sample):
            return self.client.post(
                reverse("document_create"),
                {"ocr_action": "extract", "file": make_image_file(filename)},
                follow=True,
            )

    def post_data(self, response, **overrides):
        """Build the POST the browser sends from the prefilled OCR form."""
        initial = response.context["form"].initial
        data = {}
        for name in (
            "title",
            "kind",
            "document_number",
            "issuing_authority",
            "issue_date",
            "expiry_date",
            "notes",
        ):
            value = initial.get(name)
            if isinstance(value, datetime.date):
                value = value.strftime("%Y-%m-%d")
            data[name] = value or ""
        data["ocr_assisted"] = "1"
        data["group"] = ""  # the browser submits the group select too
        data.update(overrides)
        return data

    def test_ocr_review_page_offers_the_group_dropdown(self):
        response = self.extract()
        self.assertContains(response, '<select name="group"')
        self.assertContains(response, "No Group")
        self.assertContains(response, "Car Documents")
        # Another user's group is never offered, not even after an OCR scan.
        self.assertNotContains(response, "Bob Private Group")

    def test_ocr_does_not_choose_a_group(self):
        response = self.extract()
        # No group is preselected in the extracted/initial values...
        self.assertIsNone(response.context["form"]["group"].value())
        self.assertNotIn("group", response.context["form"].initial or {})
        # ...and no option is marked as selected for the user's groups.
        for group in DocumentGroup.objects.filter(owner=self.user):
            self.assertNotContains(
                response, '<option value="%d" selected>' % group.pk, html=False
            )

    def test_ocr_assisted_add_saves_the_document_in_the_selected_group(self):
        response = self.extract()
        data = self.post_data(response, group=self.car.pk)
        response = self.client.post(reverse("document_create"), data, follow=True)
        self.assertContains(response, "Document added successfully.")
        document = Document.objects.get()
        self.assertEqual(document.owner, self.user)
        self.assertEqual(document.group, self.car)
        self.assertEqual(self.car.documents.count(), 1)

    def test_ocr_assisted_add_without_choosing_a_group_stays_ungrouped(self):
        response = self.extract()
        self.client.post(reverse("document_create"), self.post_data(response))
        document = Document.objects.get()
        self.assertIsNone(document.group)

    def test_end_to_end_ocr_flow_with_a_group_from_upload_to_detail(self):
        """Upload -> OCR -> type/fields -> choose group -> add -> group page."""
        response = self.extract()
        self.assertContains(response, "Review extracted information")
        data = self.post_data(
            response, title="Driving License - RAM EDITED", group=self.car.pk
        )
        response = self.client.post(reverse("document_create"), data, follow=True)

        document = Document.objects.get()
        self.assertEqual(document.title, "Driving License - RAM EDITED")
        self.assertEqual(document.group, self.car)
        self.assertContains(response, "Driving License - RAM EDITED")
        self.assertContains(response, "Car Documents")

        group_page = self.client.get(reverse("group_detail", args=[self.car.pk]))
        self.assertContains(group_page, "Driving License - RAM EDITED")
        # The OCR session data is still cleaned up after the save.
        self.assertNotIn("ocr_extracted_fields", self.client.session)


# ----------------------------------------------------------------------
# UX fix: adding EXISTING documents to a group in one step
# ----------------------------------------------------------------------
# The bulk-add page is deliberately limited to the user's own UNGROUPED
# documents: a document that already belongs to another group is never
# silently moved (that stays a per-document Edit action), nothing is
# duplicated or deleted, and a document still belongs to at most one group.
# ----------------------------------------------------------------------


class GroupAddExistingDocumentsTests(TestCase):
    """Add existing (ungrouped) documents to a group from the group page."""

    def setUp(self):
        self.user = create_user("alice")
        self.other = create_user("bob")
        self.client.login(username="alice", password="secret123")
        self.car = make_group(self.user, "Car Documents")
        self.bike = make_group(self.user, "Bike Documents")
        self.rc = make_document(
            self.user, title="Car RC", kind="OTHER", document_number="KA01AB1234"
        )
        self.insurance = make_document(self.user, title="Car Insurance", kind="OTHER")
        self.licence = make_document(self.user, title="Driving Licence", kind="LIC")
        self.pollution = make_document(self.user, title="Pollution Certificate")
        # Already grouped -> never offered by the bulk page.
        self.grouped = make_document(self.user, title="Bike Loan Paper", group=self.bike)
        # Another user's ungrouped document must stay out of reach.
        self.foreign = make_document(self.other, title="Bob Secret RC")
        self.url = reverse("group_add_documents", args=[self.car.pk])

    def add_documents(self, *documents):
        return self.client.post(
            self.url,
            {"documents": [document.pk for document in documents]},
            follow=True,
        )

    # -------------------------------------------------- the page itself
    def test_group_page_links_to_the_bulk_add_page(self):
        response = self.client.get(reverse("group_detail", args=[self.car.pk]))
        self.assertContains(response, "Add Existing Documents")
        self.assertContains(response, self.url)

    def test_bulk_add_page_lists_only_the_users_ungrouped_documents(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for document in (self.rc, self.insurance, self.licence, self.pollution):
            self.assertContains(response, document.title)
        # A document that already has a group is not offered...
        self.assertNotContains(response, "Bike Loan Paper")
        # ...and neither is somebody else's document.
        self.assertNotContains(response, "Bob Secret RC")

    def test_bulk_add_page_links_back_to_its_group(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Back to Car Documents")

    def test_bulk_add_page_requires_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    # ----------------------------------------------------- adding
    def test_single_existing_document_can_be_added(self):
        response = self.add_documents(self.rc)
        self.assertContains(response, "1 document added to “Car Documents”.")
        self.rc.refresh_from_db()
        self.assertEqual(self.rc.group, self.car)

    def test_multiple_documents_can_be_added_at_once(self):
        response = self.add_documents(self.rc, self.insurance, self.licence)
        self.assertContains(response, "3 documents added to “Car Documents”.")
        for document in (self.rc, self.insurance, self.licence):
            document.refresh_from_db()
            self.assertEqual(document.group, self.car)

    def test_selected_documents_appear_in_the_group_page(self):
        self.add_documents(self.rc, self.insurance, self.licence)
        response = self.client.get(reverse("group_detail", args=[self.car.pk]))
        for title in ("Car RC", "Car Insurance", "Driving Licence"):
            self.assertContains(response, title)
        self.assertNotContains(response, "Pollution Certificate")

    def test_document_detail_page_shows_the_new_group(self):
        self.add_documents(self.rc)
        response = self.client.get(reverse("document_detail", args=[self.rc.pk]))
        self.assertContains(response, "Car Documents")

    def test_unselected_documents_are_left_unchanged(self):
        self.add_documents(self.rc)
        for document in (self.insurance, self.licence, self.pollution):
            document.refresh_from_db()
            self.assertIsNone(document.group)
        self.grouped.refresh_from_db()
        self.assertEqual(self.grouped.group, self.bike)

    # ------------------------------------------------ counts + integrity
    def test_group_count_updates_after_a_bulk_add(self):
        before = self.client.get(reverse("document_list"))
        self.assertContains(before, '<span class="group-count">0</span>', html=False)
        self.add_documents(self.rc, self.insurance)
        self.assertEqual(self.car.documents.count(), 2)
        after = self.client.get(reverse("document_list"))
        self.assertContains(after, '<span class="group-count">2</span>', html=False)

    def test_no_document_records_are_created_or_deleted(self):
        before = sorted(
            Document.objects.filter(owner=self.user).values_list("pk", flat=True)
        )
        self.add_documents(self.rc, self.insurance, self.licence)
        after = sorted(
            Document.objects.filter(owner=self.user).values_list("pk", flat=True)
        )
        self.assertEqual(before, after)  # same records, just grouped
        self.assertEqual(Document.objects.filter(owner=self.other).count(), 1)

    def test_bulk_add_only_changes_the_group_field(self):
        expiry = self.rc.expiry_date
        self.add_documents(self.rc)
        self.rc.refresh_from_db()
        self.assertEqual(self.rc.group, self.car)
        self.assertEqual(self.rc.title, "Car RC")
        self.assertEqual(self.rc.kind, "OTHER")
        self.assertEqual(self.rc.document_number, "KA01AB1234")
        self.assertEqual(self.rc.expiry_date, expiry)

    # --------------------------------------------------------- security
    def test_cannot_add_documents_to_another_users_group(self):
        other_group = make_group(self.other, "Bob Private Group")
        foreign_url = reverse("group_add_documents", args=[other_group.pk])
        self.assertEqual(self.client.get(foreign_url).status_code, 404)
        self.assertEqual(
            self.client.post(foreign_url, {"documents": [self.rc.pk]}).status_code, 404
        )
        self.rc.refresh_from_db()
        self.assertIsNone(self.rc.group)
        self.assertEqual(self.car.documents.count(), 0)

    def test_another_users_document_cannot_be_added_through_tampered_post(self):
        response = self.client.post(
            self.url, {"documents": [self.foreign.pk]}, follow=True
        )
        self.assertContains(response, "not one of the available choices")
        self.foreign.refresh_from_db()
        self.assertIsNone(self.foreign.group)
        self.assertEqual(self.car.documents.count(), 0)
        self.assertEqual(Document.objects.count(), 6)  # nothing was copied

    def test_documents_in_another_group_are_not_silently_moved(self):
        response = self.client.post(
            self.url, {"documents": [self.grouped.pk]}, follow=True
        )
        self.assertContains(response, "not one of the available choices")
        self.grouped.refresh_from_db()
        self.assertEqual(self.grouped.group, self.bike)  # kept its own group
        self.assertEqual(self.car.documents.count(), 0)

    def test_mixed_selection_is_rejected_as_a_whole(self):
        response = self.client.post(
            self.url, {"documents": [self.rc.pk, self.grouped.pk]}, follow=True
        )
        self.assertContains(response, "not one of the available choices")
        self.rc.refresh_from_db()
        self.grouped.refresh_from_db()
        self.assertIsNone(self.rc.group)  # nothing partially applied
        self.assertEqual(self.grouped.group, self.bike)

    # ------------------------------------------------------ empty state
    def test_no_selection_asks_for_at_least_one_document(self):
        response = self.client.post(self.url, {}, follow=True)
        self.assertContains(response, "Select at least one document to add.")
        self.rc.refresh_from_db()
        self.assertIsNone(self.rc.group)

    def test_empty_state_when_there_are_no_ungrouped_documents(self):
        for document in (self.rc, self.insurance, self.licence, self.pollution):
            document.group = self.bike
            document.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No ungrouped documents available.")
        self.assertNotContains(response, 'name="documents"')
        self.assertNotContains(response, "Add Selected")

    def test_tampered_post_with_no_ungrouped_documents_changes_nothing(self):
        # Same state, but a forged POST: the page simply has nothing to
        # offer, and no document may change hands.
        for document in (self.rc, self.insurance, self.licence, self.pollution):
            document.group = self.bike
            document.save()
        response = self.client.post(
            self.url, {"documents": [self.foreign.pk]}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No ungrouped documents available.")
        self.foreign.refresh_from_db()
        self.assertIsNone(self.foreign.group)
        self.assertEqual(self.car.documents.count(), 0)
        self.assertEqual(self.bike.documents.count(), 5)

    # --------------------------------- the per-document path still works
    def edit_data(self, document, group):
        return {
            "title": document.title,
            "kind": document.kind,
            "group": group.pk if group else "",
            "document_number": document.document_number,
            "issuing_authority": document.issuing_authority,
            "issue_date": document.issue_date or "",
            "expiry_date": document.expiry_date or "",
            "notes": document.notes,
        }

    def test_edit_document_group_dropdown_still_works(self):
        # Move a document into the group...
        response = self.client.post(
            reverse("document_update", args=[self.insurance.pk]),
            self.edit_data(self.insurance, self.car),
            follow=True,
        )
        self.assertContains(response, "Document updated successfully.")
        self.insurance.refresh_from_db()
        self.assertEqual(self.insurance.group, self.car)

        # ...and back out of it again ("No Group").
        self.client.post(
            reverse("document_update", args=[self.insurance.pk]),
            self.edit_data(self.insurance, None),
        )
        self.insurance.refresh_from_db()
        self.assertIsNone(self.insurance.group)


# ----------------------------------------------------------------------
# UX fix: consistent, explicit back navigation
# ----------------------------------------------------------------------
# Every page with a natural parent links back to it with a real Django URL.
# Browser history (history.back()) is never used, so the links also work
# when a page is opened directly from a bookmark or a shared link.
# ----------------------------------------------------------------------


class BackNavigationTests(TestCase):
    """Each authenticated page offers a Back link to its natural parent."""

    def setUp(self):
        self.user = create_user("alice")
        self.client.login(username="alice", password="secret123")
        self.document = make_document(self.user, title="My License")
        self.group = make_group(self.user, "Car Documents")

    def assertBackLink(self, response, url, label):
        """The page must contain an anchor to ``url`` labelled ``label``."""
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        anchors = [
            anchor
            for anchor in re.findall(r"<a\b[^>]*>.*?</a>", html, re.S)
            if 'href="%s"' % url in anchor
        ]
        self.assertTrue(anchors, "no link to {} on the page".format(url))
        labels = [re.sub(r"<[^>]+>", " ", anchor) for anchor in anchors]
        self.assertTrue(
            any(label in text for text in labels),
            "{!r} is not the label of any link to {}".format(label, url),
        )

    # ------------------------------------------------- the specified pages
    def test_add_document_links_back_to_documents(self):
        response = self.client.get(reverse("document_create"))
        self.assertBackLink(response, reverse("document_list"), "Back to Documents")

    def test_edit_document_links_back_to_the_document(self):
        response = self.client.get(
            reverse("document_update", args=[self.document.pk])
        )
        self.assertBackLink(
            response,
            reverse("document_detail", args=[self.document.pk]),
            "Back to Document",
        )

    def test_document_detail_links_back_to_documents(self):
        response = self.client.get(
            reverse("document_detail", args=[self.document.pk])
        )
        self.assertBackLink(response, reverse("document_list"), "Back to Documents")

    def test_create_group_links_back_to_groups(self):
        response = self.client.get(reverse("group_create"))
        self.assertBackLink(response, reverse("group_list"), "Back to Groups")

    def test_rename_group_links_back_to_groups(self):
        response = self.client.get(reverse("group_update", args=[self.group.pk]))
        self.assertBackLink(response, reverse("group_list"), "Back to Groups")

    def test_delete_group_confirmation_links_back_to_groups(self):
        response = self.client.get(reverse("group_delete", args=[self.group.pk]))
        self.assertBackLink(response, reverse("group_list"), "Back to Groups")

    def test_group_detail_links_back_to_documents(self):
        response = self.client.get(reverse("group_detail", args=[self.group.pk]))
        self.assertBackLink(response, reverse("document_list"), "Back to Documents")

    def test_add_existing_documents_links_back_to_the_group(self):
        response = self.client.get(
            reverse("group_add_documents", args=[self.group.pk])
        )
        self.assertBackLink(
            response,
            reverse("group_detail", args=[self.group.pk]),
            "Back to Car Documents",
        )

    # --------------------------------------------- other natural parents
    def test_profile_links_back_to_dashboard(self):
        response = self.client.get(reverse("profile"))
        self.assertBackLink(response, reverse("dashboard"), "Back to Dashboard")

    def test_edit_profile_links_back_to_profile(self):
        response = self.client.get(reverse("profile_edit"))
        self.assertBackLink(response, reverse("profile"), "Back to Profile")

    def test_reminders_links_back_to_dashboard(self):
        response = self.client.get(reverse("reminders"))
        self.assertBackLink(response, reverse("dashboard"), "Back to Dashboard")

    def test_renew_document_links_back_to_the_document(self):
        response = self.client.get(reverse("document_renew", args=[self.document.pk]))
        self.assertBackLink(
            response,
            reverse("document_detail", args=[self.document.pk]),
            "Back to Document",
        )

    # ------------------------------- pages that must NOT get a back link
    def test_dashboard_has_no_back_link(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Back to ")

    def test_documents_page_has_no_back_link(self):
        response = self.client.get(reverse("document_list"))
        self.assertNotContains(response, "Back to ")

    def test_groups_page_has_no_back_link(self):
        response = self.client.get(reverse("group_list"))
        self.assertNotContains(response, "Back to ")

    # --------------------------------------- explicit URLs, not history
    def test_back_navigation_never_uses_browser_history(self):
        urls = [
            reverse("document_list"),
            reverse("document_create"),
            reverse("document_detail", args=[self.document.pk]),
            reverse("document_update", args=[self.document.pk]),
            reverse("document_renew", args=[self.document.pk]),
            reverse("group_list"),
            reverse("group_create"),
            reverse("group_detail", args=[self.group.pk]),
            reverse("group_update", args=[self.group.pk]),
            reverse("group_delete", args=[self.group.pk]),
            reverse("group_add_documents", args=[self.group.pk]),
            reverse("dashboard"),
            reverse("reminders"),
            reverse("profile"),
            reverse("profile_edit"),
        ]
        for url in urls:
            html = self.client.get(url).content.decode()
            self.assertNotIn("history.back", html, url)
            self.assertNotIn("window.history", html, url)

