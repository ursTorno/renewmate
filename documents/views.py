# Views for the documents app: list, detail, create, update, delete.
# All views require login and only ever show the logged-in user's data.

import os
import tempfile
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.files.base import File
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import get_valid_filename

from .forms import (
    GROUP_NAME_TAKEN,
    AddDocumentsToGroupForm,
    DocumentForm,
    DocumentGroupForm,
    RenewalForm,
    validate_ocr_upload,
)
from .models import Document, DocumentGroup, Renewal
from .services.document_recognition import recognize_document_type
from .services.field_extraction import extract_fields_for
from .services.ocr import OCRError, extract_text
from .services.ocr_save import (
    SUPPORTED_SAVE_TYPES,
    build_document_values,
    build_ocr_initial,
)


def _apply_document_filters(request, documents):
    """Apply the search / type / status filters from the query string.

    This is the single implementation of the Documents page filters, shared
    with the group page so filtering behaves identically in both places:
      1. free text search on title / number / authority,
      2. kind filter (a simple dropdown choice),
      3. status filter (calculated from expiry_date, never stored).
    """
    query = (request.GET.get("q") or "").strip()
    if query:
        documents = documents.filter(
            Q(title__icontains=query)
            | Q(document_number__icontains=query)
            | Q(issuing_authority__icontains=query)
        )

    kind_filter = request.GET.get("kind") or ""
    if kind_filter:
        documents = documents.filter(kind=kind_filter)

    status_filter = request.GET.get("status") or ""
    if status_filter:
        documents = [d for d in documents if d.status() == status_filter]

    return {
        "documents": documents,
        "query": query,
        "kind_filter": kind_filter,
        "status_filter": status_filter,
    }


def _groups_with_counts(user):
    """The user's groups, each with the number of THEIR documents inside.

    One query, and only the user's own documents are ever counted, so a
    group can never expose (or count) another user's documents.
    """
    return (
        DocumentGroup.objects.filter(owner=user)
        .annotate(
            document_count=Count("documents", filter=Q(documents__owner=user))
        )
        .order_by("name")
    )


@login_required
def document_list(request):
    """Show all documents of the logged-in user with search + filters."""
    filters = _apply_document_filters(
        request, Document.objects.filter(owner=request.user)
    )

    context = {
        "documents": filters["documents"],
        "query": filters["query"],
        "kind_filter": filters["kind_filter"],
        "status_filter": filters["status_filter"],
        "kind_choices": Document.KIND_CHOICES,
        # The user's own groups (with counts) for the group strip.
        "groups": _groups_with_counts(request.user),
        "current_group": None,  # "All Documents"
    }
    return render(request, "documents/document_list.html", context)


@login_required
def document_detail(request, pk):
    """Show one document. get_object_or_404 also checks the owner, so a
    user can never open someone else's document by guessing the link."""
    document = get_object_or_404(Document, pk=pk, owner=request.user)
    return render(request, "documents/document_detail.html", {"document": document})


@login_required
def document_create(request):
    """Add a new document - manually, or with OCR helping fill in the form.

    There is only ONE document entry point in RenewMate and this is it. The
    page holds the normal Add Document form plus an "Extract Details" action
    that runs the existing OCR -> recognition -> extraction pipeline and
    prefills the form for review.

    Nothing is written to the database until the user explicitly clicks
    "Add Document"; the uploaded image is kept in a private temporary folder
    until then (see the OCR helper section below).
    """
    if request.method == "POST":
        action = (request.POST.get("ocr_action") or "").strip()
        if action == "extract":
            return _handle_add_document_extract(request)
        if action == "reset":
            _clear_ocr_session(request)
            messages.info(
                request,
                "Cleared the scanned details. You can add the document manually.",
            )
            return redirect("document_create")
        return _handle_add_document_save(request)

    form = DocumentForm(initial=_ocr_form_initial(request), user=request.user)
    return render(
        request,
        "documents/document_form.html",
        _add_document_context(request, form, is_new=True),
    )


@login_required
def document_update(request, pk):
    """Edit an existing document."""
    document = get_object_or_404(Document, pk=pk, owner=request.user)
    if request.method == "POST":
        form = DocumentForm(
            request.POST, request.FILES, instance=document, user=request.user
        )
        if form.is_valid():
            form.save()
            messages.success(request, "Document updated successfully.")
            return redirect("document_detail", pk=document.pk)
        messages.error(request, "Please fix the errors below.")
    else:
        form = DocumentForm(instance=document, user=request.user)
    return render(
        request,
        "documents/document_form.html",
        {"form": form, "document": document, "is_new": False},
    )


@login_required
def document_delete(request, pk):
    """Delete a document (asks for confirmation first)."""
    document = get_object_or_404(Document, pk=pk, owner=request.user)
    if request.method == "POST":
        document.delete()
        messages.success(request, "Document deleted.")
        return redirect("document_list")
    return render(
        request, "documents/document_confirm_delete.html", {"document": document}
    )


@login_required
def document_renew(request, pk):
    """Record a renewal: move the expiry date forward and keep the history.

    Security: get_object_or_404 filters by owner, so a user can only ever
    renew their own documents (guessing another id gives a 404).
    """
    document = get_object_or_404(Document, pk=pk, owner=request.user)

    # A renewal moves an existing expiry date forward, so there must be one.
    if document.expiry_date is None:
        messages.error(request, "This document has no expiry date yet - edit it first.")
        return redirect("document_detail", pk=document.pk)

    if request.method == "POST":
        form = RenewalForm(request.POST, document=document)
        if form.is_valid():
            Renewal.objects.create(
                document=document,
                old_expiry_date=document.expiry_date,
                new_expiry_date=form.cleaned_data["new_expiry_date"],
                notes=form.cleaned_data["notes"],
            )
            document.expiry_date = form.cleaned_data["new_expiry_date"]
            document.save()
            messages.success(request, "Document renewed successfully.")
            return redirect("document_detail", pk=document.pk)
        messages.error(request, "Please fix the errors below.")
    else:
        form = RenewalForm(document=document)

    return render(
        request, "documents/renewal_form.html", {"form": form, "document": document}
    )


# ----------------------------------------------------------------------
# Document groups: user-created folders for related documents
# ----------------------------------------------------------------------
# A group is just a name the user chose ("Car Documents") plus the documents
# they put in it. Every view below looks the group up with
# ``owner=request.user``, so guessing an id gives a 404 instead of someone
# else's group. Deleting a group NEVER deletes documents: Document.group
# uses on_delete=SET_NULL, so its documents simply become ungrouped.
# ----------------------------------------------------------------------


@login_required
def group_list(request):
    """Manage the user's groups: create, rename or delete them.

    Each card shows how many of the user's documents the group holds.
    """
    return render(
        request,
        "documents/group_list.html",
        {"groups": _groups_with_counts(request.user)},
    )


@login_required
def group_create(request):
    """Create a group for the logged-in user (owner is never from the form)."""
    if request.method == "POST":
        form = DocumentGroupForm(request.POST, user=request.user)
        created = None
        if form.is_valid():
            group = form.save(commit=False)
            group.owner = request.user  # an "owner" in the POST is ignored
            try:
                # Inner atomic block: an IntegrityError (the database
                # backstop for UniqueConstraint(owner, name)) rolls back just
                # this save, so the friendly form error can still be shown.
                with transaction.atomic():
                    group.save()
            except IntegrityError:
                form.add_error("name", GROUP_NAME_TAKEN.format(group.name))
            else:
                created = group
        if created is not None:
            messages.success(request, "Group “{}” created.".format(created.name))
            return redirect("group_list")
        messages.error(request, "Please fix the errors below.")
    else:
        form = DocumentGroupForm(user=request.user)

    return render(
        request,
        "documents/group_form.html",
        {"form": form, "is_new": True},
    )


@login_required
def group_update(request, pk):
    """Rename one of the user's groups."""
    group = get_object_or_404(DocumentGroup, pk=pk, owner=request.user)

    if request.method == "POST":
        form = DocumentGroupForm(request.POST, instance=group, user=request.user)
        if form.is_valid():
            try:
                with transaction.atomic():
                    form.save()
            except IntegrityError:
                form.add_error("name", GROUP_NAME_TAKEN.format(group.name))
            else:
                messages.success(request, "Group renamed.")
                return redirect("group_list")
        messages.error(request, "Please fix the errors below.")
    else:
        form = DocumentGroupForm(instance=group, user=request.user)

    return render(
        request,
        "documents/group_form.html",
        {"form": form, "group": group, "is_new": False},
    )


@login_required
def group_delete(request, pk):
    """Delete a group (asks for confirmation first).

    The documents inside it are NOT deleted - they are only ungrouped.
    """
    group = get_object_or_404(DocumentGroup, pk=pk, owner=request.user)

    if request.method == "POST":
        name = group.name
        group.delete()  # Document.group is SET_NULL, so documents survive
        messages.success(
            request,
            "Group “{}” deleted. Its documents are still here (ungrouped).".format(name),
        )
        return redirect("group_list")

    return render(
        request,
        "documents/group_confirm_delete.html",
        {"group": group},
    )


@login_required
def group_detail(request, pk):
    """Show one group and only the documents belonging to it.

    The group is looked up with owner=request.user (another user's group is
    a 404), and the documents are filtered by owner AND group, so group
    filtering can never reveal another user's documents. Search / type /
    status keep working because the same GET filter form and the same
    filtering helper as the Documents page are reused.
    """
    group = get_object_or_404(DocumentGroup, pk=pk, owner=request.user)
    filters = _apply_document_filters(
        request, Document.objects.filter(owner=request.user, group=group)
    )

    context = {
        "documents": filters["documents"],
        "query": filters["query"],
        "kind_filter": filters["kind_filter"],
        "status_filter": filters["status_filter"],
        "kind_choices": Document.KIND_CHOICES,
        "groups": _groups_with_counts(request.user),
        "current_group": group,
    }
    return render(request, "documents/document_list.html", context)


@login_required
def group_add_documents(request, pk):
    """Add several existing (ungrouped) documents to a group in one step.

    Security: the group is looked up with owner=request.user, and the
    checkbox form's queryset only contains the user's own documents that
    have no group yet. The final update repeats both filters, so a tampered
    id can never pull in another user's document or steal a document out of
    a different group. Documents are only ever UPDATED - nothing is deleted
    or duplicated, and a document still belongs to at most one group.
    """
    group = get_object_or_404(DocumentGroup, pk=pk, owner=request.user)
    available = Document.objects.filter(
        owner=request.user, group__isnull=True
    ).order_by("title")

    if request.method == "POST":
        form = AddDocumentsToGroupForm(request.POST, user=request.user)
        if form.is_valid():
            chosen = form.cleaned_data["documents"]
            count = Document.objects.filter(
                owner=request.user,
                group__isnull=True,
                pk__in=[document.pk for document in chosen],
            ).update(group=group, updated_at=timezone.now())
            if count:
                messages.success(
                    request,
                    "{} {} added to “{}”.".format(
                        count, "document" if count == 1 else "documents", group.name
                    ),
                )
                return redirect("group_detail", pk=group.pk)
            messages.info(request, "Those documents are already grouped.")
            return redirect("group_detail", pk=group.pk)
        messages.error(request, "Please fix the errors below.")
    else:
        form = AddDocumentsToGroupForm(user=request.user)

    return render(
        request,
        "documents/group_add_documents.html",
        {"form": form, "group": group, "available_documents": available},
    )


# ----------------------------------------------------------------------
# OCR helpers (upload -> OCR -> recognition -> extraction -> review)
# ----------------------------------------------------------------------
# The Add Document page is the single OCR entry point:
#   1. the user uploads an image and clicks "Extract Details" (or the small
#      JS helper clicks it as soon as a file is chosen),
#   2. the existing services extract the text, recognise the document type
#      and pull out the document specific fields,
#   3. the Add Document form is prefilled so the user can review/verify it,
#   4. ONLY the explicit "Add Document" click creates a Document.
#
# Until that click nothing is written to the database: the OCR result and
# the extracted fields live in the user's session, and the uploaded image
# is kept in a private per-session temporary folder so it can be attached
# when (and only when) the Document is created.
#
# The standalone OCR Scanner page was removed from the user interface - the
# OCR services themselves (ocr.py, document_recognition.py,
# field_extraction.py, ocr_save.py) are unchanged and reused here.
# ----------------------------------------------------------------------

# Every OCR-specific session key (authentication data is never touched).
OCR_SESSION_KEYS = (
    "ocr_result",
    "ocr_filename",
    "ocr_csrf_token",
    "ocr_document_type",
    "ocr_document_label",
    "ocr_confidence",
    "ocr_document_icon",
    "ocr_extracted_fields",
    "ocr_status",
    "ocr_error_message",
)

# Pending scans live OUTSIDE MEDIA_ROOT (in the system temp folder) so they
# can never be served as media files to other users.
OCR_PENDING_DIRNAME = "renewmate_ocr_pending"

# What the template shows after an extraction attempt:
#   "review"    -> fields were extracted; show the detection + review alert
#   "no_fields" -> the document was not recognised as a supported type
#   "failed"    -> OCR itself failed; manual entry only
OCR_STATUS_REVIEW = "review"
OCR_STATUS_NO_FIELDS = "no_fields"
OCR_STATUS_FAILED = "failed"

# Friendly message shown when OCR could not pull structured details out.
OCR_NO_FIELDS_MESSAGE = (
    "OCR completed, but RenewMate couldn't identify supported details "
    "in this document. You can enter the details manually."
)
OCR_FAILED_MESSAGE = (
    "OCR couldn't process this image. You can enter the details manually."
)


def _ocr_pending_root():
    """Root folder that holds pending scan images (per session)."""
    return Path(tempfile.gettempdir()) / OCR_PENDING_DIRNAME


def _safe_pending_path(path):
    """True only for paths inside our own pending folder (defence in depth:
    a path is always written by this module, never read from the browser)."""
    try:
        candidate = Path(path).resolve()
        candidate.relative_to(_ocr_pending_root().resolve())
        return True
    except (TypeError, ValueError, OSError):
        return False


def _delete_pending_scan(request):
    """Remove this session's pending scan image, if one is waiting."""
    path = request.session.pop("ocr_file_path", None)
    if path and _safe_pending_path(path):
        try:
            Path(path).unlink(missing_ok=True)
            parent = Path(path).parent
            root = _ocr_pending_root().resolve()
            if parent.resolve() != root and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass  # a leftover temp file is never fatal


def _store_pending_scan(request, uploaded):
    """Keep the uploaded image for this session's review/save step only.

    The file is stored under a folder named after the session key, so one
    user can never read another user's pending scan.
    """
    _delete_pending_scan(request)  # a new scan replaces any previous one
    session_key = request.session.session_key or "anonymous"
    directory = _ocr_pending_root() / get_valid_filename(session_key)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        safe_name = (
            get_valid_filename(os.path.basename(uploaded.name or "")) or "scan.jpg"
        )
        target = directory / safe_name
        with open(target, "wb") as handle:
            for chunk in uploaded.chunks():
                handle.write(chunk)
    except OSError:
        return  # saving the document without a file is supported
    request.session["ocr_file_path"] = str(target)


def _clear_ocr_session(request):
    """Drop all OCR session data (and the pending file) - nothing else."""
    _delete_pending_scan(request)  # also pops "ocr_file_path"
    for key in OCR_SESSION_KEYS:
        request.session.pop(key, None)


# ----------------------------------------------------------------------
# Add Document + OCR: view helpers
# ----------------------------------------------------------------------


def _ocr_form_initial(request):
    """Prefill the Add Document form from an extracted (but unsaved) scan.

    This reuses the Stage 4 mapping in ``services/ocr_save.py``: the
    extracted values are mapped onto the EXISTING Document fields, and
    anything the model cannot store is kept readable in ``notes``. There is
    no second mapping implementation.
    """
    doc_type = request.session.get("ocr_document_type")
    stored = request.session.get("ocr_extracted_fields")
    if doc_type in SUPPORTED_SAVE_TYPES and isinstance(stored, dict):
        return build_document_values(doc_type, build_ocr_initial(doc_type, stored))
    return None


def _add_document_context(request, form, is_new):
    """Template context for the Add Document page (incl. the OCR state)."""
    doc_type = request.session.get("ocr_document_type")
    status = request.session.get("ocr_status")
    recognized = None
    if doc_type:
        recognized = {
            "document_type": doc_type,
            "label": request.session.get("ocr_document_label") or doc_type,
            "confidence": request.session.get("ocr_confidence") or "Low",
            "icon": request.session.get("ocr_document_icon") or "bi-file-earmark",
        }
    return {
        "form": form,
        "is_new": is_new,
        "recognized": recognized,
        "ocr_status": status,
        "ocr_filename": request.session.get("ocr_filename"),
        # Only a successful structured extraction shows the review alert and
        # the "already added" duplicate guard.
        "ocr_assisted": status == OCR_STATUS_REVIEW,
        "ocr_no_fields_message": (
            request.session.get("ocr_error_message") or OCR_FAILED_MESSAGE
            if status == OCR_STATUS_FAILED
            else OCR_NO_FIELDS_MESSAGE
        ),
    }


def _handle_add_document_extract(request):
    """Run the existing OCR pipeline for an upload on the Add Document page.

    Nothing is saved here. On success the extracted values are stored in the
    session and the user goes back to the (prefilled) Add Document form.
    """
    uploaded = request.FILES.get("file")
    if not uploaded:
        messages.error(
            request, "Choose a document image first, then extract the details."
        )
        return redirect("document_create")

    try:
        validate_ocr_upload(uploaded)
    except ValidationError as exc:
        for message in exc.messages:
            messages.error(request, message)
        return redirect("document_create")

    # A new attempt replaces any previous scan (and deletes its temp file).
    _clear_ocr_session(request)

    try:
        ocr = extract_text(uploaded)
    except OCRError as exc:
        # OCR failed - keep the file so the document can still be added by
        # hand with its scan attached. Manual entry is never blocked.
        _store_pending_scan(request, uploaded)
        request.session["ocr_filename"] = uploaded.name
        request.session["ocr_status"] = OCR_STATUS_FAILED
        request.session["ocr_error_message"] = str(exc)
        return redirect("document_create")

    # Existing services: rule-based recognition, then field extraction.
    recognized = recognize_document_type(ocr.text)
    request.session["ocr_result"] = ocr.text
    request.session["ocr_filename"] = uploaded.name
    request.session["ocr_csrf_token"] = getattr(request, "csrf_token", None)
    request.session["ocr_document_type"] = recognized["document_type"]
    request.session["ocr_document_label"] = recognized["label"]
    request.session["ocr_confidence"] = recognized["confidence"]
    request.session["ocr_document_icon"] = recognized["icon"]

    extraction = extract_fields_for(ocr.text, recognized["document_type"])
    fields = extraction["fields"] if isinstance(extraction, dict) else None
    if isinstance(fields, dict) and any(fields.values()):
        request.session["ocr_extracted_fields"] = fields
        request.session["ocr_status"] = OCR_STATUS_REVIEW
    else:
        # Recognised nothing useful (or not a supported type): manual entry.
        request.session["ocr_status"] = OCR_STATUS_NO_FIELDS

    _store_pending_scan(request, uploaded)
    return redirect("document_create")


def _handle_add_document_save(request):
    """Create the Document from the Add Document form (manual or OCR filled).

    This runs for every Add Document POST, so manual creation behaves
    exactly as it always has. The owner is ALWAYS request.user - an
    ``owner``/``owner_id`` value in the POST data is never read.
    """
    if request.POST.get("ocr_assisted") == "1" and _ocr_form_initial(request) is None:
        # The same scanned document was already added (double click or a
        # repeated POST): never create a second copy.
        messages.info(request, "This scanned document has already been added.")
        return redirect("document_create")

    form = DocumentForm(request.POST, request.FILES, user=request.user)
    if not form.is_valid():
        messages.error(request, "Please fix the errors below.")
        return render(
            request,
            "documents/document_form.html",
            _add_document_context(request, form, is_new=True),
        )

    document = form.save(commit=False)
    document.owner = request.user  # the document belongs to this user

    # If no fresh file was chosen, attach the scan that was used for OCR (it
    # was stored privately so it can become the Document.file).
    pending_path = request.session.get("ocr_file_path")
    attach_pending = (
        not form.cleaned_data.get("file")
        and pending_path
        and _safe_pending_path(pending_path)
        and Path(pending_path).is_file()
    )

    # One transaction so a failure cannot leave a half-created record.
    with transaction.atomic():
        if attach_pending:
            try:
                with open(pending_path, "rb") as handle:
                    document.file.save(
                        request.session.get("ocr_filename") or "scan.jpg",
                        File(handle),
                        save=False,
                    )
            except OSError:
                pass  # the document is still added, just without the image
        document.save()

    _clear_ocr_session(request)  # also removes the temporary file

    messages.success(request, "Document added successfully.")
    return redirect("document_detail", pk=document.pk)

