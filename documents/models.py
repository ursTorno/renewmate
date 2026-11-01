# Models for the documents app.
# The Document model is the heart of RenewMate: it stores one personal
# document and the dates needed to track its renewal.

from django.conf import settings
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils import timezone


class DocumentGroup(models.Model):
    """A user-created group for related documents (e.g. "Car Documents").

    A group is a pure user organisation choice: it belongs to exactly one
    user, its name is completely free text, and a document may live in NO
    group at all. The same name may be used by different users, but a single
    user cannot create the same group name twice (see Meta.constraints).
    """

    # Every group belongs to exactly one user. Deleting the user deletes
    # their groups (CASCADE); the documents themselves are removed by the
    # Document.owner cascade, never by their group.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        # Named "document_groups" (not "groups") because the auth User model
        # already owns a "groups" relation to django.contrib.auth.Group.
        related_name="document_groups",
    )
    name = models.CharField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "document group"
        verbose_name_plural = "document groups"
        # The same user cannot accidentally create duplicate group names.
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name"],
                name="unique_group_name_per_owner",
            )
        ]

    def __str__(self):
        return self.name


class Document(models.Model):
    """One personal document owned by a user, with its renewal dates."""

    # Choices for the kind of document (used in filters and badges).
    KIND_CHOICES = [
        ("ID", "ID Proof"),
        ("CERT", "Certificate"),
        ("LIC", "License"),
        ("PASSPORT", "Passport"),
        ("OTHER", "Other"),
    ]

    # Every document belongs to exactly one user. When the user is deleted
    # their documents are deleted too (CASCADE).
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    title = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="OTHER")
    # Optional user-created group ("Car Documents"). A document may have NO
    # group (null=True) and deleting a group must never delete documents:
    # SET_NULL simply makes its documents ungrouped.
    group = models.ForeignKey(
        DocumentGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    document_number = models.CharField(max_length=60, blank=True)
    issuing_authority = models.CharField(max_length=120, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    # Optional scan / photo of the document (stored in media/documents).
    file = models.FileField(
        upload_to="documents/",
        blank=True,
        validators=[
            FileExtensionValidator(
                allowed_extensions=["pdf", "png", "jpg", "jpeg", "webp"]
            )
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["expiry_date", "title"]
        verbose_name_plural = "documents"

    def __str__(self):
        return self.title

    # -----------------------------------------------------------------
    # Status helpers (plain Python, easy to read and test)
    # -----------------------------------------------------------------
    def days_left(self):
        """Whole days until expiry (negative when already expired)."""
        if self.expiry_date is None:
            return None
        from django.utils import timezone

        today = timezone.localdate()
        return (self.expiry_date - today).days

    def status(self):
        """Return one of: valid, expiring, expired, unknown."""
        days = self.days_left()
        if days is None:
            return "unknown"
        if days < 0:
            return "expired"
        if days <= 30:
            return "expiring"
        return "valid"

    def get_days_text(self):
        """Human friendly sentence, e.g. 'Expires in 7 days'."""
        days = self.days_left()
        if days is None:
            return "No expiry date set"
        if days < 0:
            overdue = abs(days)
            unit = "day" if overdue == 1 else "days"
            return "Expired {} {} ago".format(overdue, unit)
        if days == 0:
            return "Expires today"
        unit = "day" if days == 1 else "days"
        return "Expires in {} {}".format(days, unit)


class Renewal(models.Model):
    """One recorded renewal of a document (old expiry date -> new expiry date).

    Kept separate from Document so the full history stays available even
    after the document's expiry_date moves forward.
    """

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="renewals",
    )
    old_expiry_date = models.DateField()
    new_expiry_date = models.DateField()
    renewed_on = models.DateField(default=timezone.localdate)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-renewed_on", "-created_at"]  # newest renewal first

    def __str__(self):
        return "Renewal #{} for '{}'".format(self.pk, self.document.title)

