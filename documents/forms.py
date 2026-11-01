# Forms for the documents app.
# DocumentForm turns the Document model into an HTML form with validation.

from django import forms

from .models import Document, DocumentGroup

# Shown when a user tries to reuse one of their own group names (the form
# check and the database constraint backstop share this message).
GROUP_NAME_TAKEN = "You already have a group called “{}”."


class DocumentGroupForm(forms.ModelForm):
    """Create / rename a user-created group (only the name is editable)."""

    class Meta:
        model = DocumentGroup
        fields = ["name"]
        labels = {"name": "Group name"}
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. Car Documents"}
            )
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # The owner is never taken from the form - it is always the logged-in
        # user (the view sets it), and it is used here to check duplicates.
        self.user = user

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Please enter a group name.")

        # A single user must not create the same group name twice. The model
        # has the matching UniqueConstraint(owner, name) as a database
        # backstop, but the friendly message belongs here. The check ignores
        # letter case so "Car Documents" and "car documents" are the same
        # group for this user (the form is the only way to create groups).
        if self.user is not None:
            duplicates = DocumentGroup.objects.filter(
                owner=self.user, name__iexact=name
            )
            if self.instance.pk:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                raise forms.ValidationError(GROUP_NAME_TAKEN.format(name))
        return name


class UngroupedDocumentMultipleChoiceField(forms.ModelMultipleChoiceField):
    """Document choices shown as "Title — Type" in the checkbox list."""

    def label_from_instance(self, document):
        return "{} — {}".format(document.title, document.get_kind_display())


class BootstrapCheckboxSelectMultiple(forms.CheckboxSelectMultiple):
    """CheckboxSelectMultiple that uses Bootstrap's form-check-input class."""

    def create_option(self, *args, **kwargs):
        option = super().create_option(*args, **kwargs)
        css_class = option["attrs"].get("class")
        option["attrs"]["class"] = (
            "{} form-check-input".format(css_class) if css_class else "form-check-input"
        )
        return option


class AddDocumentsToGroupForm(forms.Form):
    """Pick which of the user's UNGROUPED documents to add to a group.

    The queryset is the security boundary: it contains only the logged-in
    user's own documents that have no group yet. A tampered id (another
    user's document, or a document that already belongs to a different
    group) is therefore rejected as an invalid choice instead of being
    moved, and nothing is ever duplicated.
    """

    documents = UngroupedDocumentMultipleChoiceField(
        queryset=Document.objects.none(),
        widget=BootstrapCheckboxSelectMultiple,
        label="Ungrouped documents",
        help_text="Only documents that have no group yet are listed.",
        error_messages={"required": "Select at least one document to add."},
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        documents = Document.objects.none()
        if user is not None:
            documents = Document.objects.filter(
                owner=user, group__isnull=True
            ).order_by("title")
        self.fields["documents"].queryset = documents


class DocumentForm(forms.ModelForm):
    """Add / edit form for a Document (fields shown in the HTML form)."""

    class Meta:
        model = Document
        fields = [
            "title",
            "kind",
            # The user's own groups only (never OCR): see __init__ below.
            "group",
            "document_number",
            "issuing_authority",
            "issue_date",
            "expiry_date",
            "notes",
            "file",
        ]
        labels = {"kind": "Document type", "group": "Group"}
        widgets = {
            "title": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. Driving License"}
            ),
            "kind": forms.Select(attrs={"class": "form-select"}),
            "group": forms.Select(attrs={"class": "form-select"}),
            "document_number": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. KA0520110001234"}
            ),
            "issuing_authority": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. RTO Bengaluru"}
            ),
            "issue_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}
            ),
            "expiry_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}
            ),
            "notes": forms.Textarea(
                attrs={"class": "form-control", "rows": 3}
            ),
            "file": forms.FileInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        """Scope the "Group" dropdown to the logged-in user's own groups.

        Security: a ModelForm ForeignKey would otherwise list every group of
        every user, and would accept any group id posted from the browser.
        Restricting the queryset to ``owner=user`` makes another user's group
        an invalid choice, so it can never be assigned to a document. Without
        a user (defensive default) no group is offered at all.
        """
        super().__init__(*args, **kwargs)
        groups = DocumentGroup.objects.none()
        if user is not None:
            groups = DocumentGroup.objects.filter(owner=user)
        self.fields["group"].queryset = groups
        # A document may always stay ungrouped ("No Group" = empty value).
        self.fields["group"].empty_label = "No Group"
        self.fields["group"].required = False

    # Validation rules run automatically when is_valid() is called.
    def clean_title(self):
        title = self.cleaned_data.get("title", "").strip()
        if not title:
            raise forms.ValidationError("Please enter a title.")
        return title

    def clean(self):
        cleaned = super().clean()
        issue_date = cleaned.get("issue_date")
        expiry_date = cleaned.get("expiry_date")
        # Expiry must come after the issue date (when both are given).
        if issue_date and expiry_date and expiry_date < issue_date:
            raise forms.ValidationError(
                "Expiry date must be on or after the issue date."
            )
        return cleaned


class RenewalForm(forms.Form):
    """Form to renew a document: enter the new expiry date (and notes).

    The current expiry date and the 'renewed on' date are taken from the
    document and today respectively - the user only decides the NEW expiry.
    """

    new_expiry_date = forms.DateField(
        label="New expiry date",
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    notes = forms.CharField(
        label="Notes",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "e.g. Renewed online for two more years.",
            }
        ),
    )

    def __init__(self, *args, document=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.document = document

    def clean_new_expiry_date(self):
        """A renewal only moves the expiry forward - never backwards."""
        new_date = self.cleaned_data["new_expiry_date"]
        current = self.document.expiry_date if self.document else None
        if current and new_date <= current:
            raise forms.ValidationError(
                "The new expiry date must be after the current expiry date ({}).".format(
                    current.strftime("%d %b %Y")
                )
            )
        return new_date


# ----------------------------------------------------------------------
# OCR upload validation (shared by Add Document and OcrScanForm)
# ----------------------------------------------------------------------
# The existing OCR services (documents/services/ocr.py) read raster images,
# so one set of rules is used everywhere a user uploads something to scan.
# Keeping the rules in one place means the Add Document page and the
# OcrScanForm can never drift apart - the upload is validated server-side.
OCR_UPLOAD_EXTENSIONS = ("jpg", "jpeg", "png", "webp")
OCR_UPLOAD_MAX_BYTES = 5 * 1024 * 1024


def validate_ocr_upload(uploaded):
    """Validate a file that is about to be scanned. Returns the upload.

    Raises forms.ValidationError with a user friendly message. Used by the
    Add Document page (its file input also owns the final Document.file)
    and by OcrScanForm.
    """
    if not uploaded:
        return uploaded

    name = (getattr(uploaded, "name", "") or "").lower()
    extension = name.rsplit(".", 1)[-1] if "." in name else ""
    if extension not in OCR_UPLOAD_EXTENSIONS:
        raise forms.ValidationError(
            "Unsupported file type. Upload a JPG, JPEG, PNG or WEBP image."
        )
    if getattr(uploaded, "size", 0) > OCR_UPLOAD_MAX_BYTES:
        raise forms.ValidationError("File is too large. Upload an image up to 5 MB.")
    return uploaded


class OcrScanForm(forms.Form):
    """Upload an image to scan with OCR. No document fields are touched.

    This form only owns file validation; the OCR engine lives in
    ``documents/services/ocr.py``.
    """

    scan_file = forms.FileField(
        label="Document image",
        help_text="JPG, JPEG, PNG or WEBP. Up to 5 MB.",
        widget=forms.ClearableFileInput(
            attrs={"class": "form-control", "accept": ".jpg,.jpeg,.png,.webp"}
        ),
    )

    def clean_scan_file(self):
        return validate_ocr_upload(self.cleaned_data.get("scan_file"))