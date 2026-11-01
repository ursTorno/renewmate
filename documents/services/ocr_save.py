# Connect extracted OCR results to the EXISTING Document model.
#
# Pipeline: upload (Add Document) -> extraction (field_extraction.py)
# -> these helpers map the values -> the Add Document form is prefilled
# -> the user reviews/edits and clicks "Add Document"
# -> Document.objects.create(owner=request.user, ...)
#
# Only pure, database-free helpers live here: the view performs the actual
# save (always with owner=request.user) after DocumentForm validation.
# Nothing is ever saved automatically; saving is an explicit user action.

import datetime

from .field_extraction import PRIMARY_IDENTIFIER_KEYS

# Only these recognized types can be saved (others stay manual entry).
# Mirrors the supported set of PRIMARY_IDENTIFIER_KEYS in
# field_extraction.py so the two layers cannot drift apart.
SUPPORTED_SAVE_TYPES = (
    "driving_license",
    "vehicle_insurance",
    "vehicle_registration",
)

# Mapping onto the EXISTING Document.KIND_CHOICES. No new choices are
# introduced: a driving license is a License ("LIC"), while insurance
# policies and registration certificates have no dedicated kind yet, so
# the safest compatible value is the catch-all "OTHER". The document type
# is still visible in the generated title and in the notes.
OCR_KIND_MAP = {
    "driving_license": "LIC",
    "vehicle_insurance": "OTHER",
    "vehicle_registration": "OTHER",
}

# Date formats accepted from the review form. The extraction stage produces
# DD/MM/YYYY strings; the browser date inputs submit ISO YYYY-MM-DD.
OCR_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y")

# Which form fields are dates, per document type (used to prepare the
# initial values of the review form).
_DATE_FIELD_NAMES = {
    "driving_license": ("date_of_birth", "issue_date", "expiry_date"),
    "vehicle_insurance": ("start_date", "expiry_date"),
    "vehicle_registration": ("registration_date",),
}


def parse_ocr_date(value):
    """Parse an extracted/form date string into a real date, or None.

    A real datetime.date is used for every date field so an invalid date
    (e.g. 31/02/2024) simply becomes None instead of a broken record.
    """
    if isinstance(value, datetime.date):
        return value
    value = (value or "").strip()
    if not value:
        return None
    for date_format in OCR_DATE_FORMATS:
        try:
            return datetime.datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return None


def build_ocr_initial(doc_type, fields):
    """Session extracted fields -> initial values for the review form.

    Date fields are converted to real date objects so the <input type=date>
    widgets render ISO values; unparseable dates become None (empty input)
    instead of a value the browser cannot display.
    """
    fields = fields or {}
    date_names = _DATE_FIELD_NAMES.get(doc_type, ())
    initial = {}
    for key, value in fields.items():
        if key in date_names:
            initial[key] = parse_ocr_date(value)
        else:
            initial[key] = value or ""
    return initial


def _notes_block(pairs):
    """Join 'Label: value' lines, skipping empty values."""
    lines = [
        "{}: {}".format(label, value)
        for label, value in pairs
        if value
    ]
    return "\n".join(lines)


def _fmt_date(value):
    """Display dates the way the documents print them (DD/MM/YYYY)."""
    if isinstance(value, datetime.date):
        return value.strftime("%d/%m/%Y")
    return value or ""


def _document_number(doc_type, data):
    """Map the document-specific primary identifier -> document_number.

    The identifier key comes from PRIMARY_IDENTIFIER_KEYS (single source of
    truth in field_extraction.py): ``license_number`` for a driving licence,
    ``policy_number`` for insurance, ``registration_number`` for an RC -
    and, once their extractors are added, ``passport_number``,
    ``aadhaar_number``, ``pan_number`` and ``certificate_number`` for the
    future types. Nothing is invented when the identifier was not
    extracted: the field simply stays empty.
    """
    key = PRIMARY_IDENTIFIER_KEYS.get(doc_type)
    if not key:
        return ""
    return (data.get(key) or "").strip()


def build_document_values(doc_type, data):
    """Map extracted values onto the EXISTING Document model fields.

    ``data`` is the extracted-fields dict run through :func:`build_ocr_initial`
    (dates are real date objects or None) and is used to prefill the Add
    Document form. Returns keyword arguments for Document() - without
    ``owner``, which only the view may set (always request.user).

    Extracted information the model cannot store (names, classes, vehicle
    details...) is kept readable in ``notes`` instead of being discarded.
    """
    data = data or {}
    if doc_type == "driving_license":
        name = (data.get("name") or "").strip()
        title = "Driving License - {}".format(name) if name else "Driving License"
        notes = _notes_block(
            (
                ("Name", name),
                ("Date of Birth", _fmt_date(data.get("date_of_birth"))),
                ("Vehicle Class", data.get("vehicle_class") or ""),
            )
        )
        return {
            "title": title[:120],
            "kind": OCR_KIND_MAP["driving_license"],
            "document_number": _document_number("driving_license", data),
            "issuing_authority": "",
            "issue_date": data.get("issue_date"),
            "expiry_date": data.get("expiry_date"),
            "notes": notes,
        }

    if doc_type == "vehicle_insurance":
        holder = (data.get("policy_holder") or "").strip()
        title = (
            "Vehicle Insurance - {}".format(holder) if holder else "Vehicle Insurance"
        )
        notes = _notes_block(
            (
                ("Policy Holder", holder),
                ("Vehicle Number", data.get("vehicle_number") or ""),
                ("Insurer", data.get("insurer") or ""),
                ("IDV", data.get("idv") or ""),
                ("Premium", data.get("premium") or ""),
            )
        )
        return {
            "title": title[:120],
            "kind": OCR_KIND_MAP["vehicle_insurance"],
            "document_number": _document_number("vehicle_insurance", data),
            "issuing_authority": "",
            "issue_date": data.get("start_date"),
            "expiry_date": data.get("expiry_date"),
            "notes": notes,
        }

    if doc_type == "vehicle_registration":
        owner_name = (data.get("owner_name") or "").strip()
        title = "Vehicle RC - {}".format(owner_name) if owner_name else "Vehicle RC"
        notes = _notes_block(
            (
                ("Owner Name", owner_name),
                ("Vehicle Class", data.get("vehicle_class") or ""),
                ("Fuel Type", data.get("fuel_type") or ""),
                ("Manufacturer", data.get("manufacturer") or ""),
                ("Model", data.get("model") or ""),
                ("Chassis Number", data.get("chassis_number") or ""),
                ("Engine Number", data.get("engine_number") or ""),
            )
        )
        return {
            "title": title[:120],
            "kind": OCR_KIND_MAP["vehicle_registration"],
            "document_number": _document_number("vehicle_registration", data),
            "issuing_authority": "",
            "issue_date": data.get("registration_date"),
            # An RC has no validity end date in our extraction - never
            # invent one. expiry_date stays empty.
            "expiry_date": None,
            "notes": notes,
        }

    raise ValueError("Unsupported document type: {}".format(doc_type))
