# Stage 3A + 3B: rule-based field extraction for supported types.
#
# Pipeline: Upload -> OCR (ocr.py) -> raw text -> recognition
# -> extract_fields_for() [driving_license, vehicle_insurance,
# vehicle_registration] -> primary identifier -> DocumentForm.document_number
#
# No AI, no cloud calls, no new dependencies: regex + labels + date
# validation. Missing or uncertain fields return "" (never guessed).
# Nothing here touches the database; the result lives in the session.

import datetime
import re

# Words that prove a "name" candidate is a document header, not a person.
_BLOCKED_NAME_WORDS = (
    "driving",
    "licence",
    "license",
    "government",
    "india",
    "transport",
    "department",
    "republic",
    "passport",
    "certificate",
    "authority",
    "regional",
    "office",
    "valid",
    "date",
    "birth",
    "issue",
    "expiry",
    "number",
    "card",
    "aadhaar",
    "uidai",
    "income",
    "tax",
)

# Labels that mark each field (case-insensitive).
_NAME_LABEL_RE = re.compile(
    r"(?:name(?:\s+of\s+(?:the\s+)?holder)?|holder\s+name)\s*[:\-]?",
    re.IGNORECASE,
)
_RELATION_RE = re.compile(
    r"\b[SDW]\s*/\s*O\s*[:\-]?\s*([A-Za-z][A-Za-z\s\.\']{1,60})",
    re.IGNORECASE,
)
# The DL value must stay inside ONE line: newlines never belong to a number.
_DL_LABEL_RE = re.compile(
    r"(?:dl\s*no\.?|dl\s*number|driving\s+licen[sc]e\s+no\.?|licen[sc]e\s+no\.?|"
    r"licen[sc]e\s+number|license\s+number|license\s+no)"
    r"\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9 \-\t]{5,24})",
    re.IGNORECASE,
)
_DOB_LABEL_RE = re.compile(
    r"\b(d\.?\s*o\.?\s*b\.?|date\s+of\s+birth|birth\s+date)\b",
    re.IGNORECASE,
)
_ISSUE_LABEL_RE = re.compile(
    r"\b(date\s+of\s+issue|issue\s+date|issued\s+on|valid\s+from)\b",
    re.IGNORECASE,
)
_EXPIRY_LABEL_RE = re.compile(
    r"\b(valid\s+(till|to|up\s*to|up\s?to)|expiry\s+date|date\s+of\s+expiry|"
    r"validity)\b",
    re.IGNORECASE,
)
_CLASS_LABEL_RE = re.compile(
    r"\b(class\s+of\s+vehicle|vehicle\s+class|cov|category)\b",
    re.IGNORECASE,
)
_CLASS_TOKEN_RE = re.compile(
    r"\b(MCWG|MCWOG|HGMV|HPMV|LMV[\s\-]?NT|LMV|HMV|HPV)\b",
    re.IGNORECASE,
)
# "VALID UPTO" uses regex alternation order: longest "up to" first.
_VALID_UPTO_FALLBACK_RE = re.compile(r"\bvalid\s+up\s*to\b", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2,4})\b")
# Words inside a name value where the actual name ends (more labels follow).
_NAME_STOP_RE = re.compile(
    r"\b(d\.?\s*o\.?\s*b\.?|date\s+of\s+birth|dl\s*no|licen[sc]e\s+no|"
    r"valid|date|address)\b",
    re.IGNORECASE,
)


def _clean_name(value):
    """Tidy a raw name string; return "" when it is not a real person name."""
    value = (value or "").strip(" :-.,")
    stop = _NAME_STOP_RE.search(value)
    if stop:
        value = value[: stop.start()]
    value = re.sub(r"\s+", " ", value).strip(" :-.,")
    if not (3 <= len(value) <= 60):
        return ""
    if not re.fullmatch(r"[A-Za-z\s\.\']+", value):
        return ""
    lowered = value.lower()
    if any(word in lowered for word in _BLOCKED_NAME_WORDS):
        return ""
    return value


def _extract_name(lines):
    """Name comes from an explicit Name label, or above the S/O line."""
    for i, line in enumerate(lines):
        match = _NAME_LABEL_RE.search(line)
        if match:
            # Value may follow the label on the same line...
            candidate = _clean_name(line[match.end():])
            if candidate:
                return candidate
            # ...or sit on the next non-empty line.
            for following in lines[i + 1:]:
                if following.strip():
                    return _clean_name(following)
            return ""
    # Fallback: holder name is usually printed just above "S/O ..." line.
    for i, line in enumerate(lines):
        if _RELATION_RE.search(line) and i > 0:
            for previous in reversed(lines[:i]):
                if previous.strip():
                    return _clean_name(previous)
            return ""
    return ""


def _extract_license_number(text):
    """License number only counts when a DL label is next to it."""
    match = _DL_LABEL_RE.search(text)
    if not match:
        return ""
    number = re.sub(r"[ \-\t]", "", match.group(1)).upper()
    if not (8 <= len(number) <= 20):
        return ""
    if not re.match(r"^[A-Z]{2}", number):
        return ""  # Indian DL numbers start with a 2-letter state code
    if not re.search(r"[A-Z]", number) or not re.search(r"\d", number):
        return ""
    return number


def _parse_date(day, month, year):
    """Build a real calendar date; None when impossible (e.g. 31/02/2025)."""
    try:
        day, month, year = int(day), int(month), int(year)
        if year < 100:
            # Two-digit years on licenses are expiry dates in the 2000s.
            year += 2000
        if not (1900 <= year <= 2100):
            return None
        return datetime.date(year, month, day)
    except (ValueError, TypeError):
        return None


def _extract_dates(lines):
    """Dates are assigned by the nearest label, never by position order."""
    found = {"date_of_birth": "", "issue_date": "", "expiry_date": ""}
    parsed = {}
    has_strong_expiry = any(_EXPIRY_LABEL_RE.search(line) for line in lines)
    previous_label = None
    for line in lines:
        labels = []  # (position, kind) of every label on this line
        for regex, kind in (
            (_DOB_LABEL_RE, "date_of_birth"),
            (_ISSUE_LABEL_RE, "issue_date"),
            (_EXPIRY_LABEL_RE, "expiry_date"),
        ):
            for match in regex.finditer(line):
                labels.append((match.start(), kind))
        if not has_strong_expiry:
            # "VALID UPTO 12/03/2027" is common on real licenses: treat the
            # date after it as the expiry date.
            for match in _VALID_UPTO_FALLBACK_RE.finditer(line):
                labels.append((match.start(), "expiry_date"))
        for match in _DATE_RE.finditer(line):
            date = _parse_date(match.group(1), match.group(2), match.group(3))
            if date is None:
                continue  # invalid calendar date: skip it
            # Two-digit years are a guess, so only a date ON THE SAME LINE
            # as its label may set a field; unlabeled guesses never set one.
            same_line_labels = [(p, k) for p, k in labels if p < match.start()]
            kind = sorted(same_line_labels)[-1][1] if same_line_labels else None
            # ...otherwise fall back to the previous line's label.
            if kind is None and previous_label in found:
                kind = previous_label
            if kind and not found[kind]:
                found[kind] = date.strftime("%d/%m/%Y")
                parsed[kind] = date
        if labels:  # remember this line's last label for the next line
            previous_label = sorted(labels)[-1][1]
        elif _DATE_RE.search(line):
            previous_label = None  # a dateless line resets the carry-over
    # Expiry must not be earlier than issue; drop it only when the issue
    # date itself is certain (a four-digit year), never on a guess.
    if (
        parsed.get("issue_date")
        and parsed.get("expiry_date")
        and parsed["expiry_date"] < parsed["issue_date"]
        and parsed["issue_date"].year >= 1000
    ):
        found["expiry_date"] = ""
    return found


def _extract_vehicle_class(lines):
    """Known class codes near a class label (e.g. 'Class of Vehicle')."""
    tokens = []
    for i, line in enumerate(lines):
        context = line + " " + (lines[i + 1] if i + 1 < len(lines) else "")
        if not _CLASS_LABEL_RE.search(context):
            continue
        for match in _CLASS_TOKEN_RE.finditer(context):
            token = re.sub(r"[\s\-]", "", match.group(1)).upper()
            if token == "LMVNT":
                token = "LMV-NT"
            if token not in tokens:
                tokens.append(token)
        if re.search(r"\bnon[\s\-]?transport\b", context, re.IGNORECASE):
            if "Non Transport" not in tokens:
                tokens.append("Non Transport")
        elif re.search(r"(?<!non )(?<!non-)transport\b", context, re.IGNORECASE):
            if "Transport" not in tokens:
                tokens.append("Transport")
    return ", ".join(tokens)


# ----------------------------------------------------------------------
# Primary unique identifier (the value RenewMate shows as "Document Number")
# ----------------------------------------------------------------------
# Every document has ONE primary unique identifier: a DL number, an
# insurance policy number, an RC registration number, a passport number...
#
# The document-specific extractors below return it under their OWN key
# (license_number, policy_number, registration_number, ...). This registry is
# the single place that records which key that is, so the OCR -> DocumentForm
# mapping layer can fill RenewMate's generic ``Document.document_number``
# without knowing anything about document types:
#
#     document-specific field -> primary identifier -> document_number
#
# Adding a new document type later = write its extractor + one line here.
PRIMARY_IDENTIFIER_KEYS = {
    "driving_license": "license_number",
    "vehicle_insurance": "policy_number",
    "vehicle_registration": "registration_number",
    # Recognition already knows these types; their extractors are not
    # written yet, but the mapping below is ready for them.
    "passport": "passport_number",
    "aadhaar": "aadhaar_number",
    "pan": "pan_number",
    "certificate": "certificate_number",
}

# Key used by the GENERIC identifier rule below. That rule works for any
# document type purely from label evidence and is only ever a fallback: when
# the document-specific rule finds nothing, this may still supply the number
# - but only with strong evidence, never by guessing.
GENERIC_IDENTIFIER_KEY = "document_number"

# A generic candidate must sit next to a label that ENDS with one of these
# words (punctuation removed): "Policy No.", "Passport Number", "Document
# ID", "Serial No", "Reference No", "Registration No"...
_IDENTIFIER_LABEL_ENDINGS = ("no", "nos", "number", "numbers", "num", "id")

# Words in a label that make it strong evidence of a DOCUMENT identifier.
_IDENTIFIER_STRONG_LABEL_WORDS = (
    "document",
    "certificate",
    "licence",
    "license",
    "passport",
    "aadhaar",
    "aadhar",
    "pan",
    "policy",
    "registration",
    "serial",
    "reference",
    "ref",
    "application",
    "enrolment",
    "enrollment",
    "dl",
    "uid",
)

# Labels that must NEVER be read as the document number: these belong to
# other people/things/amounts printed on the document. This is what stops the
# extractor from guessing a phone number, a PIN code, a date, an amount, an
# engine number, a chassis number, an address and so on.
_IDENTIFIER_BLOCKED_LABEL_WORDS = (
    "phone",
    "mobile",
    "contact",
    "tel",
    "fax",
    "whatsapp",
    "address",
    "pin",
    "postal",
    "zip",
    "email",
    "website",
    "account",
    "ifsc",
    "branch",
    "bank",
    "engine",
    "chassis",
    "vin",
    "seat",
    "wheel",
    "tyre",
    "fuel",
    "vehicle",
    "model",
    "maker",
    "manufacturer",
    "capacity",
    "cc",
    "kw",
    "weight",
    "height",
    "amount",
    "premium",
    "idv",
    "tax",
    "gst",
    "invoice",
    "receipt",
    "salary",
    "balance",
    "total",
    "date",
    "dob",
    "birth",
    "valid",
    "expiry",
    "issue",
    "age",
    "gender",
    "blood",
    "nominee",
    "relation",
    "name",
)

# Longest identifier we accept, so a paragraph can never become the number.
_IDENTIFIER_MAX_LENGTH = 32
_IDENTIFIER_CURRENCY_RE = re.compile(r"[₹$€£]|^rs\.?", re.IGNORECASE)
_IDENTIFIER_DATE_LIKE_RE = re.compile(r"^\d{1,4}[/\-.]\d{1,2}([/\-.]\d{1,4})?$")
_IDENTIFIER_YEAR_RE = re.compile(r"^\d{4}$")
_IDENTIFIER_SEPARATOR_RE = re.compile(r"[:#]")
_IDENTIFIER_TOKEN_RE = re.compile(r"\S+")


def _normalise_label(label):
    """Lower-case a label and drop punctuation ("Policy No." -> "policy no")."""
    label = re.sub(r"[^A-Za-z0-9\s/]+", " ", (label or "").lower())
    return re.sub(r"\s+", " ", label).strip()


def _is_plausible_identifier(value):
    """True only for something that really looks like a document identifier.

    Conservative on purpose: a phone number, PIN code, date, amount, engine
    capacity or address must never pass this test.
    """
    if not value or len(value) > _IDENTIFIER_MAX_LENGTH:
        return False
    if _IDENTIFIER_CURRENCY_RE.search(value):
        return False
    letters = len(re.findall(r"[A-Za-z]", value))
    digits = len(re.findall(r"\d", value))
    if not digits:
        return False
    if letters == 0:
        # Pure digits: long enough to be an identifier (an Aadhaar number is
        # 12 digits) - never a 4-6 digit PIN, height, seat count or CC value.
        if digits < 8:
            return False
    else:
        if len(value) < 5 or letters < 1:
            return False
    if _IDENTIFIER_DATE_LIKE_RE.match(value) or _IDENTIFIER_YEAR_RE.match(value):
        return False
    if len(set(value)) < 3:
        return False  # "000000", "AAAA"
    return True


def _clean_generic_identifier(value):
    """Turn the text after a label into a candidate identifier, or ''."""
    value = (value or "").strip().strip(".,;:#()[]{}<>\"'")
    if not value:
        return ""
    # Only the first comma/semicolon-free chunk is considered, so a list such
    # as "milk, eggs, bread" can never be merged into one value.
    chunk = re.split(r"[,;|]", value)[0].strip()
    if not chunk:
        return ""
    if re.fullmatch(r"\d[\d\s\-]*", chunk):
        # Digit groups with spaces/hyphens ("1234 5678 9012" -> Aadhaar).
        candidate = re.sub(r"[\s\-]", "", chunk)
    else:
        candidate = chunk.split()[0]
    candidate = candidate.strip(".,;:#()[]{}<>\"'")
    if not _is_plausible_identifier(candidate):
        return ""
    return candidate


def _split_identifier_label(line):
    """Split a line into (label, value) around an identifier-style label.

    Returns (None, "") when the line carries no identifier label at all.
    """
    separator = _IDENTIFIER_SEPARATOR_RE.search(line)
    if separator:
        label = line[: separator.start()].strip()
        value = line[separator.end():].strip()
    else:
        # No separator: the label runs up to the LAST label word such as
        # "No"/"Number"/"ID" (e.g. "Registration No AP39AB1234").
        end = None
        for match in _IDENTIFIER_TOKEN_RE.finditer(line):
            token = re.sub(r"[^A-Za-z]", "", match.group(0)).lower()
            if token in _IDENTIFIER_LABEL_ENDINGS:
                end = match.end()
        if end is None:
            return None, ""
        label = line[:end].strip()
        value = line[end:].strip()
    words = _normalise_label(label).split()
    if not words or words[-1] not in _IDENTIFIER_LABEL_ENDINGS:
        return None, ""
    return label, value


def extract_primary_identifier(text):
    """Find the primary unique identifier of ANY document, from its labels.

    Generic rule (used when the document-specific rule finds nothing): look
    for an identifier label ("No.", "Number", "ID", "Document ID",
    "Reference No", "Registration No", "Certificate No", "Serial No"...) and
    accept the value only when it looks like an identifier.

    Returns ``None`` when there is no such evidence - it never guesses a
    random number from the page (phone, PIN, date, amount, engine/chassis
    number, address...). A label containing a document keyword (passport,
    licence, policy, registration, certificate, serial, reference...) wins
    over a bare "No."/"Number"/"ID"; otherwise the first plausible value is
    returned as ``{"value": ..., "evidence": "label"}``.
    """
    if not (text or "").strip():
        return None
    lines = [line.strip() for line in text.splitlines()]
    weak = ""
    for i, line in enumerate(lines):
        if not line:
            continue
        label, value = _split_identifier_label(line)
        if label is None:
            continue
        words = _normalise_label(label).split()
        if any(word in _IDENTIFIER_BLOCKED_LABEL_WORDS for word in words):
            continue
        if not value:
            # The value may sit on the next non-empty line.
            for following in lines[i + 1:]:
                if following.strip():
                    value = following
                    break
        candidate = _clean_generic_identifier(value)
        if not candidate:
            continue
        if any(word in _IDENTIFIER_STRONG_LABEL_WORDS for word in words):
            return {"value": candidate, "evidence": "strong_label"}
        if not weak:
            weak = candidate  # plausible; keep looking for a better label
    if not weak:
        # No identifier evidence at all: never invent a number from a
        # phone, PIN code, date, amount, address or any other stray value.
        return None
    return {"value": weak, "evidence": "label"}


def extract_driving_license_fields(text):
    """Extract Driving License fields from raw OCR text.

    Returns {"document_type": "Driving License", "fields": {...}} where
    every missing/uncertain field is "".
    """
    fields = {
        "name": "",
        "license_number": "",
        "date_of_birth": "",
        "issue_date": "",
        "expiry_date": "",
        "vehicle_class": "",
    }
    if not (text or "").strip():
        return {"document_type": "Driving License", "fields": fields}
    lines = [line.strip() for line in text.splitlines()]
    fields["name"] = _extract_name(lines)
    fields["license_number"] = _extract_license_number(text)
    fields.update(_extract_dates(lines))
    fields["vehicle_class"] = _extract_vehicle_class(lines)
    return {"document_type": "Driving License", "fields": fields}


# Words that prove an insurance "name" is a heading/company, not a person.
_INS_BLOCKED_HOLDER_WORDS = _BLOCKED_NAME_WORDS + (
    "insurance",
    "motor",
    "policy",
    "policyholder",
    "holder",
    "company",
    "limited",
    "ltd",
    "private",
    "pvt",
    "insurer",
    "provider",
    "certificate",
    "document",
    "cover",
    "plan",
)

_INS_POLICY_LABEL_RE = re.compile(
    r"\b(policy\s*(no\.?|number|id)|certificate\s*(no\.?|number))\b",
    re.IGNORECASE,
)
_INS_HOLDER_LABEL_RE = re.compile(
    r"\b(policy\s*holder|policyholder|insured\s*name|name\s*of\s*insured|"
    r"proposer\s*name|owner\s*name)\b",
    re.IGNORECASE,
)
# The vehicle value must stay inside ONE line.
_INS_VEHICLE_LABEL_RE = re.compile(
    r"\b(vehicle\s*(no\.?|number)|registration\s*(no\.?|number)|"
    r"regn\.?\s*no\.?|reg\.?\s*no\.?)\b\s*[:\-]?",
    re.IGNORECASE,
)
_INS_VEHICLE_VALUE_RE = re.compile(
    r"[A-Z]{2}[\s\-]*\d{1,2}[\s\-]*[A-Z]{1,3}[\s\-]*\d{1,4}",
    re.IGNORECASE,
)
_INS_VEHICLE_STRICT_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{1,4}$")
_INS_INSURER_LABEL_RE = re.compile(
    r"\b(insurance\s*company|insurer|insured\s*by|insurance\s*provider|"
    r"insurance\s*co\.?)\b\s*[:\-]?",
    re.IGNORECASE,
)


def _ins_clean_policy_number(value):
    """Keep letters/digits plus - and /; drop stray punctuation/words."""
    value = (value or "").strip(" :-.,")
    value = re.sub(r"[^A-Za-z0-9/\-]", " ", value)
    value = re.sub(r"\s+", "", value).strip("-/").upper()
    if not (6 <= len(value) <= 32):
        return ""
    if not re.search(r"\d", value):
        return ""
    if re.search(r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}", value):
        return ""  # a date, not a policy number
    return value


def _ins_extract_policy_number(lines):
    """Policy number only counts when a policy label is next to it."""
    for i, line in enumerate(lines):
        match = _INS_POLICY_LABEL_RE.search(line)
        if not match:
            continue
        candidate = _ins_clean_policy_number(line[match.end():])
        if candidate:
            return candidate
        # The number may sit on the next non-empty line.
        for following in lines[i + 1:]:
            if following.strip():
                return _ins_clean_policy_number(following)
        return ""
    return ""


def _ins_extract_holder(lines):
    """Holder name only counts next to a holder label (never a heading)."""
    for i, line in enumerate(lines):
        match = _INS_HOLDER_LABEL_RE.search(line)
        if not match:
            continue
        candidate = _clean_name(line[match.end():])
        lowered = candidate.lower()
        if candidate and not any(w in lowered for w in _INS_BLOCKED_HOLDER_WORDS):
            return candidate
        for following in lines[i + 1:]:
            if following.strip():
                candidate = _clean_name(following)
                lowered = candidate.lower()
                if candidate and not any(
                    w in lowered for w in _INS_BLOCKED_HOLDER_WORDS
                ):
                    return candidate
                return ""
        return ""
    return ""


def _ins_extract_vehicle_number(lines):
    """Vehicle number only counts next to a vehicle/registration label."""
    for i, line in enumerate(lines):
        match = _INS_VEHICLE_LABEL_RE.search(line)
        if not match:
            continue
        candidates = [line[match.end():]]
        for following in lines[i + 1:]:
            if following.strip():
                candidates.append(following)
                break
        for candidate in candidates:
            found = _INS_VEHICLE_VALUE_RE.search(candidate)
            if not found:
                continue
            normalized = re.sub(r"[\s\-]", "", found.group(0)).upper()
            if _INS_VEHICLE_STRICT_RE.match(normalized):
                return normalized
        # Label exists but no valid registration nearby: do not guess.
        for candidate in candidates:
            if candidate.strip():
                return ""
        return ""
    return ""


def _ins_extract_insurer(lines):
    """Company name next to an insurer label; generic headings are dropped."""
    blocked = ("motor insurance", "insurance policy", "policy document")
    for i, line in enumerate(lines):
        match = _INS_INSURER_LABEL_RE.search(line)
        if not match:
            continue
        candidate = re.sub(r"\s+", " ", line[match.end():]).strip(" :-.,")
        if not candidate:
            for following in lines[i + 1:]:
                if following.strip():
                    candidate = re.sub(r"\s+", " ", following).strip(" :-.,")
                    break
        lowered = candidate.lower()
        if not (3 <= len(candidate) <= 80):
            return ""
        if any(phrase in lowered for phrase in blocked):
            return ""
        if not re.search(r"[A-Za-z]", candidate):
            return ""
        return candidate
    return ""


_INS_START_LABEL_RE = re.compile(
    r"\b(policy\s*(period|start(\s*date)?)|period\s*of\s*insurance|"
    r"start\s*date|effective\s*from|valid\s*from|commencement\s*date)\b",
    re.IGNORECASE,
)
_INS_FROM_LABEL_RE = re.compile(r"(?<![A-Za-z])from\b", re.IGNORECASE)
_INS_END_LABEL_RE = re.compile(
    r"\b(policy\s*(end(\s*date)?|expiry)|expiry\s*date|end\s*date|"
    r"valid\s*(till|to|up\s*to)|policy\s*valid\s*(till|to|up\s*to))\b",
    re.IGNORECASE,
)


def _ins_find_dates_on_line(line):
    """Valid calendar dates found on one line (invalid ones are skipped)."""
    found = []
    for match in _DATE_RE.finditer(line):
        date = _parse_date(match.group(1), match.group(2), match.group(3))
        if date is not None:
            found.append((match.start(), date))
    return found


def _ins_extract_period(lines):
    """Start/end dates from period labels; a bare 'From X To Y' also counts.

    Never assigns by position alone: every date needs a label on its line
    (or a 'From/To' range on one line).
    """
    start, end = "", ""
    parsed_start, parsed_end = None, None
    joined = "\n".join(lines)
    has_start_label = bool(_INS_START_LABEL_RE.search(joined))
    # First pass: explicit start/end labels anywhere in the document.
    for line in lines:
        start_labels = [
            (m.start(), "start_date") for m in _INS_START_LABEL_RE.finditer(line)
        ]
        start_labels += [
            (m.start(), "start_date") for m in _INS_FROM_LABEL_RE.finditer(line)
        ]
        end_labels = [
            (m.start(), "end_date") for m in _INS_END_LABEL_RE.finditer(line)
        ]
        dates = _ins_find_dates_on_line(line)
        # A "From 01/01/2025 To 31/12/2025" range on ONE line is reliable.
        from_match = _INS_FROM_LABEL_RE.search(line)
        if from_match and len(dates) >= 2:
            to_match = re.search(r"(?<![A-Za-z])to\b", line, re.IGNORECASE)
            ordered = sorted(dates)
            if to_match:
                before = [d for pos, d in ordered if pos < to_match.start()]
                after = [d for pos, d in ordered if pos > to_match.start()]
                if before and after and not start:
                    start, parsed_start = (
                        before[0].strftime("%d/%m/%Y"),
                        before[0],
                    )
                if before and after and not end:
                    end, parsed_end = after[0].strftime("%d/%m/%Y"), after[0]
                continue
        for position, date in dates:
            kind = None
            for label_pos, label_kind in sorted(start_labels + end_labels):
                if label_pos < position:
                    kind = label_kind
            if kind == "start_date" and not start:
                start, parsed_start = date.strftime("%d/%m/%Y"), date
            elif kind == "end_date" and not end:
                end, parsed_end = date.strftime("%d/%m/%Y"), date
    # Second pass: 'Policy Period: 01/01/2025 - 31/12/2025' style ranges on
    # the label line itself (or the label with dates on the next line).
    if not (start and end) and has_start_label:
        for i, line in enumerate(lines):
            dates = _ins_find_dates_on_line(line)
            if not dates:
                continue
            context = line
            if i + 1 < len(lines):
                context += " " + lines[i + 1]
            if not _INS_START_LABEL_RE.search(context):
                continue
            ordered = sorted(dates)
            if not start:
                start, parsed_start = (
                    ordered[0][1].strftime("%d/%m/%Y"),
                    ordered[0][1],
                )
            if len(ordered) > 1 and not end:
                end, parsed_end = (
                    ordered[-1][1].strftime("%d/%m/%Y"),
                    ordered[-1][1],
                )
    # Expiry must not be earlier than a certain (4-digit year) start date.
    if (
        parsed_start
        and parsed_end
        and parsed_end < parsed_start
        and parsed_start.year >= 1000
    ):
        end = ""
    return {"start_date": start, "expiry_date": end}


_INS_IDV_LABEL_RE = re.compile(
    r"\b(insured\s*declared\s*value|idv)\b", re.IGNORECASE
)
_INS_PREMIUM_LABEL_RE = re.compile(
    r"\b((total|gross|net)\s*premium|premium\s*(amount|payable)?)\b",
    re.IGNORECASE,
)
_INS_MONEY_RE = re.compile(
    r"(?:rs\.?|inr|₹)?\s*([\d][\d,]*(?:\.\d{1,2})?)", re.IGNORECASE
)


def _ins_clean_money(value):
    """Keep a plain numeric money string (digits + optional decimals)."""
    value = (value or "").strip(" :-.,")
    match = _INS_MONEY_RE.search(value)
    if not match:
        return ""
    digits = re.sub(r",", "", match.group(1))
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", digits):
        return ""
    try:
        amount = float(digits)
    except ValueError:
        return ""
    if amount <= 0 or amount > 100000000:
        return ""
    return digits


def _ins_extract_money(lines, label_regex, other_label_regex):
    """Money value next to its label; never from the other money's line."""
    for i, line in enumerate(lines):
        match = label_regex.search(line)
        if not match:
            continue
        candidates = [line[match.end():]]
        for following in lines[i + 1:]:
            if following.strip():
                # Stop if the next line belongs to the other money field.
                if other_label_regex.search(following):
                    break
                candidates.append(following)
                break
        for candidate in candidates:
            # Skip candidates that mention the OTHER field (e.g. "IDV ...
            # Premium ..." on one line): ambiguous, leave empty.
            if other_label_regex.search(candidate):
                continue
            cleaned = _ins_clean_money(candidate)
            if cleaned:
                return cleaned
        return ""
    return ""


def extract_vehicle_insurance_fields(text):
    """Extract Vehicle Insurance fields from raw OCR text.

    Returns {"document_type": "Vehicle Insurance", "fields": {...}} where
    every missing/uncertain field is "".
    """
    fields = {
        "policy_number": "",
        "policy_holder": "",
        "vehicle_number": "",
        "insurer": "",
        "start_date": "",
        "expiry_date": "",
        "idv": "",
        "premium": "",
    }
    if not (text or "").strip():
        return {"document_type": "Vehicle Insurance", "fields": fields}
    lines = [line.strip() for line in text.splitlines()]
    fields["policy_number"] = _ins_extract_policy_number(lines)
    fields["policy_holder"] = _ins_extract_holder(lines)
    fields["vehicle_number"] = _ins_extract_vehicle_number(lines)
    fields["insurer"] = _ins_extract_insurer(lines)
    fields.update(_ins_extract_period(lines))
    fields["idv"] = _ins_extract_money(
        lines, _INS_IDV_LABEL_RE, _INS_PREMIUM_LABEL_RE
    )
    fields["premium"] = _ins_extract_money(
        lines, _INS_PREMIUM_LABEL_RE, _INS_IDV_LABEL_RE
    )
    return {"document_type": "Vehicle Insurance", "fields": fields}


# ----------------------------------------------------------------------
# Stage 3C: Vehicle Registration / RC field extraction
# ----------------------------------------------------------------------

# Owner labels (specific labels first, bare "Owner:" / "Name:" second pass).
_RC_OWNER_LABEL_RE = re.compile(
    r"\b(owner\s*name|name\s*of\s*owner|registered\s*owner)\b\s*[:\-]?",
    re.IGNORECASE,
)
_RC_OWNER_BARE_RE = re.compile(
    r"(?<![A-Za-z])(?:owner|name)\s*:\s*", re.IGNORECASE
)
_RC_REG_LABEL_RE = re.compile(
    r"\b(registration\s*(no\.?|number)|regn\.?\s*no\.?|reg\.?\s*no\.?|"
    r"vehicle\s+registration\s*(no\.?|number)|vehicle\s*(no\.?|number)|"
    r"registration)\b\s*[:\-]?",
    re.IGNORECASE,
)
_RC_REG_VALUE_RE = re.compile(
    # BH-series plates ("AP-24-BH-1234-AB") come first: ordered alternation,
    # otherwise "BH" would partially match the generic letter group.
    r"[A-Z]{2}[\s\-]*\d{1,2}[\s\-]*BH[\s\-]*\d{3,4}[\s\-]*[A-Z]{1,2}"
    r"|[A-Z]{2}[\s\-]*\d{1,2}[\s\-]*[A-Z]{1,3}[\s\-]*\d{1,4}",
    re.IGNORECASE,
)
_RC_REG_STRICT_RE = re.compile(
    r"^[A-Z]{2}\d{1,2}(?:BH\d{3,4}[A-Z]{1,2}|[A-Z]{1,3}\d{1,4})$"
)
_RC_REG_DATE_LABEL_RE = re.compile(
    r"\b(date\s+of\s+registration|registration\s+date|regn\.?\s*date|"
    r"reg\.?\s*date|registration\s+dt\.?)\b\s*[:\-]?",
    re.IGNORECASE,
)
_RC_CLASS_LABEL_RE = re.compile(
    r"\b(vehicle\s+class|class\s+of\s+vehicle|cov|class)\b\s*[:\-]?",
    re.IGNORECASE,
)
_RC_CLASS_VALUE_RE = re.compile(
    r"\b(MCWG|MCWOG|HGMV|HPMV|LMV[\s\-]?NT|LMV|HMV|moto?r\s*car|"
    r"moto?r\s*cyc?le|heavy\s+motor\s+vehicle|goods\s+carrier|"
    r"passenger\s+vehicle|three\s+wheelers?|transport|non[\s\-]?transport)\b",
    re.IGNORECASE,
)
_RC_FUEL_LABEL_RE = re.compile(r"\bfuel\s*(type)?\b\s*[:\-]?", re.IGNORECASE)
_RC_FUEL_VALUE_RE = re.compile(
    r"\b(petrol|diesel|cng|lpg|electric|eletric|hybrid)\b", re.IGNORECASE
)
_RC_FUEL_CANON = {
    "petrol": "PETROL",
    "diesel": "DIESEL",
    "cng": "CNG",
    "lpg": "LPG",
    "electric": "ELECTRIC",
    "eletric": "ELECTRIC",
    "hybrid": "HYBRID",
}
_RC_MAKER_LABEL_RE = re.compile(
    r"\b(manufacturer\s*name|manufacturer|maker'?s?\s*name|make)\b\s*[:\-]?",
    re.IGNORECASE,
)
_RC_MODEL_LABEL_RE = re.compile(
    r"\b(model\s*name|vehicle\s*model|model)\b\s*[:\-]?",
    re.IGNORECASE,
)
# "Maker / Model: TATA NEXON" mixes two fields on one line: skip both.
_RC_MAKER_MODEL_COMBINED_RE = re.compile(
    r"\b(maker|make|manufacturer)\s*/\s*(model|class)\b", re.IGNORECASE
)
_RC_CHASSIS_LABEL_RE = re.compile(
    r"\bchassis\s*(?:no\.?|number)?\b\s*[:\.\-]?", re.IGNORECASE
)
_RC_ENGINE_LABEL_RE = re.compile(
    r"\bengine\s*(?:no\.?|number)?\b\s*[:\.\-]?"
    r"(?!\s*(?:power|capacity|displacement)\b)",
    re.IGNORECASE,
)
_RC_LONG_CODE_RE = re.compile(r"[A-Z0-9][A-Z0-9\-]{4,39}", re.IGNORECASE)
# Any RC field label (used to stop a fallback that would grab another field).
_RC_LABEL_ON_LINE_RE = re.compile(
    r"\b(chassis|engine|fuel|model|maker|manufacturer|owner|name|"
    r"registration|vehicle|class)\b",
    re.IGNORECASE,
)
# Words that prove a "maker/model" candidate is a heading, not a value.
_RC_BLOCKED_MAKER_WORDS = (
    "registration",
    "certificate",
    "government",
    "ministry",
    "road",
    "transport",
    "highway",
    "department",
    "vehicle",
)


def _rc_candidates(lines, i, match):
    """Text after a label plus the next non-empty line (as a fallback)."""
    values = [lines[i][match.end():]]
    for following in lines[i + 1:]:
        if following.strip():
            values.append(following)
            break
    return values


def _rc_clean_registration_number(candidate):
    """Return a compact Indian registration number, or '' when invalid."""
    found = _RC_REG_VALUE_RE.search(candidate)
    if not found:
        return ""
    normalized = re.sub(r"[\s\-]", "", found.group(0)).upper()
    if _RC_REG_STRICT_RE.match(normalized):
        return normalized
    return ""


def _rc_extract_registration_number(lines):
    """Registration number only counts next to a registration label.

    Keeps scanning (instead of stopping on the first label) because RC text
    starts with headers like "REGISTRATION CERTIFICATE" before the real
    "Registration No: AP39AB1234" line.
    """
    for i, line in enumerate(lines):
        match = _RC_REG_LABEL_RE.search(line)
        if not match:
            continue
        for candidate in _rc_candidates(lines, i, match):
            number = _rc_clean_registration_number(candidate)
            if number:
                return number
    return ""


def _rc_extract_owner(lines):
    """Owner name only counts next to an owner label; headings are dropped."""
    for i, line in enumerate(lines):
        match = _RC_OWNER_LABEL_RE.search(line)
        if not match:
            continue
        for candidate in _rc_candidates(lines, i, match):
            name = _clean_name(candidate)
            if name:
                return name
        return ""
    # Second pass: a bare "Owner:" / "Name:" label (never "Name of ...").
    for i, line in enumerate(lines):
        match = _RC_OWNER_BARE_RE.search(line)
        if not match:
            continue
        remainder = line[match.end():]
        if re.match(r"\s*of\b", remainder, re.IGNORECASE):
            continue
        for candidate in _rc_candidates(lines, i, match):
            name = _clean_name(candidate)
            if name:
                return name
    return ""


def _rc_extract_registration_date(lines):
    """Registration date only counts next to a registration-date label."""
    for i, line in enumerate(lines):
        match = _RC_REG_DATE_LABEL_RE.search(line)
        if not match:
            continue
        for candidate in _rc_candidates(lines, i, match):
            date_match = _DATE_RE.search(candidate)
            if not date_match:
                continue
            date = _parse_date(
                date_match.group(1), date_match.group(2), date_match.group(3)
            )
            if date is not None:
                return date.strftime("%d/%m/%Y")
        return ""
    return ""


def _rc_normalize_class(value):
    """Canonical class text: codes uppercase, phrases title-cased."""
    key = re.sub(r"[\s\-]+", "", (value or "").lower())
    canonical = {
        "mcwg": "MCWG",
        "mcwog": "MCWOG",
        "hgmv": "HGMV",
        "hpmv": "HPMV",
        "lmv": "LMV",
        "lmvnt": "LMV-NT",
        "hmv": "HMV",
        "motorcar": "Motor Car",
        "motorcycle": "Motor Cycle",
        "heavymotorvehicle": "Heavy Motor Vehicle",
        "goodscarrier": "Goods Carrier",
        "passengervehicle": "Passenger Vehicle",
        "threewheeler": "Three Wheeler",
        "transport": "Transport",
        "nontransport": "Non Transport",
    }
    return canonical.get(key, "")


def _rc_extract_vehicle_class(lines):
    """Class codes or phrases near a class label (same + next line)."""
    found = []
    for i, line in enumerate(lines):
        context = line + " " + (lines[i + 1] if i + 1 < len(lines) else "")
        if not _RC_CLASS_LABEL_RE.search(context):
            continue
        for match in _RC_CLASS_VALUE_RE.finditer(context):
            value = _rc_normalize_class(match.group(1))
            if value and value not in found:
                found.append(value)
    return ", ".join(found)


def _rc_extract_fuel_type(lines):
    """Fuel value near a fuel label; canonical uppercase form."""
    for i, line in enumerate(lines):
        match = _RC_FUEL_LABEL_RE.search(line)
        if not match:
            continue
        for candidate in _rc_candidates(lines, i, match):
            value = _RC_FUEL_VALUE_RE.search(candidate)
            if value:
                return _RC_FUEL_CANON.get(value.group(1).lower(), "")
        return ""
    return ""


def _rc_clean_titled(value):
    """Clean a maker/model value; '' for headings or non-value text."""
    value = (value or "").strip(" :-.,")
    value = re.sub(r"\s+", " ", value).strip(" :-.,")
    if not (2 <= len(value) <= 80):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\s\.\&\'\-]{1,79}", value):
        return ""
    if not re.search(r"[A-Za-z]", value):
        return ""  # a bare number like "12345" is never a maker/model
    lowered = value.lower()
    if any(word in lowered for word in _RC_BLOCKED_MAKER_WORDS):
        return ""
    return value


def _rc_cut_at_label(remainder):
    """Truncate the text after a label at the next RC field label."""
    for match in _RC_LABEL_ON_LINE_RE.finditer(remainder):
        return remainder[:match.start()]
    return remainder


def _rc_extract_maker(lines):
    """Manufacturer near a maker label; combined 'Maker/Model' is skipped."""
    for i, line in enumerate(lines):
        if _RC_MAKER_MODEL_COMBINED_RE.search(line):
            continue
        match = _RC_MAKER_LABEL_RE.search(line)
        if not match:
            continue
        candidate = _rc_clean_titled(_rc_cut_at_label(line[match.end():]))
        if candidate:
            return candidate
        for following in lines[i + 1:]:
            if following.strip():
                if _RC_LABEL_ON_LINE_RE.search(following):
                    return ""
                return _rc_clean_titled(following)
        return ""
    return ""


def _rc_extract_model(lines):
    """Model name near a model label; combined 'Maker/Model' is skipped."""
    for i, line in enumerate(lines):
        if _RC_MAKER_MODEL_COMBINED_RE.search(line):
            continue
        match = _RC_MODEL_LABEL_RE.search(line)
        if not match:
            continue
        candidate = _rc_clean_titled(_rc_cut_at_label(line[match.end():]))
        if candidate:
            return candidate
        for following in lines[i + 1:]:
            if following.strip():
                if _RC_LABEL_ON_LINE_RE.search(following):
                    return ""
                return _rc_clean_titled(following)
        return ""
    return ""


def _rc_clean_code(value):
    """Compact alphanumeric chassis/engine code; '' when not code-like."""
    value = re.sub(r"[\s\-]", "", (value or "").upper())
    if not (6 <= len(value) <= 40):
        return ""
    if not re.search(r"[A-Z]", value) or not re.search(r"\d", value):
        return ""
    return value


def _rc_extract_long_code(lines, label_regex):
    """A long alphanumeric code near a chassis/engine label only."""
    for i, line in enumerate(lines):
        match = label_regex.search(line)
        if not match:
            continue
        values = [line[match.end():]]
        for following in lines[i + 1:]:
            if following.strip():
                values.append(following)
                break
        for index, candidate in enumerate(values):
            # A fallback line that is itself another field label is not
            # the value (e.g. "Chassis No" followed by "Engine No: ...").
            if index > 0 and _RC_LABEL_ON_LINE_RE.search(candidate):
                continue
            code_match = _RC_LONG_CODE_RE.search(candidate)
            if not code_match:
                continue
            code = _rc_clean_code(code_match.group(0))
            if code:
                return code
        return ""
    return ""


def _rc_extract_chassis_number(lines):
    """Chassis number near a chassis label."""
    return _rc_extract_long_code(lines, _RC_CHASSIS_LABEL_RE)


def _rc_extract_engine_number(lines):
    """Engine number near an engine label (never 'Engine Power')."""
    return _rc_extract_long_code(lines, _RC_ENGINE_LABEL_RE)


def extract_vehicle_registration_fields(text):
    """Extract Vehicle Registration / RC fields from raw OCR text.

    Returns {"document_type": "Vehicle Registration / RC", "fields": {...}}
    where every missing / uncertain field is "".
    """
    fields = {
        "registration_number": "",
        "owner_name": "",
        "registration_date": "",
        "vehicle_class": "",
        "fuel_type": "",
        "manufacturer": "",
        "model": "",
        "chassis_number": "",
        "engine_number": "",
    }
    if not (text or "").strip():
        return {"document_type": "Vehicle Registration / RC", "fields": fields}
    lines = [line.strip() for line in text.splitlines()]
    fields["registration_number"] = _rc_extract_registration_number(lines)
    fields["owner_name"] = _rc_extract_owner(lines)
    fields["registration_date"] = _rc_extract_registration_date(lines)
    fields["vehicle_class"] = _rc_extract_vehicle_class(lines)
    fields["fuel_type"] = _rc_extract_fuel_type(lines)
    fields["manufacturer"] = _rc_extract_maker(lines)
    fields["model"] = _rc_extract_model(lines)
    fields["chassis_number"] = _rc_extract_chassis_number(lines)
    fields["engine_number"] = _rc_extract_engine_number(lines)
    return {"document_type": "Vehicle Registration / RC", "fields": fields}


def extract_fields_for(text, document_type_slug):
    """Run extraction only for supported types; None for everything else.

    The currently supported types are driving_license, vehicle_insurance
    and vehicle_registration. Passport, Aadhaar, PAN and certificate are
    wired into PRIMARY_IDENTIFIER_KEYS but their extractors do not exist
    yet (a later stage adds them), so they return None here too.
    """
    if document_type_slug == "driving_license":
        return extract_driving_license_fields(text)
    if document_type_slug == "vehicle_insurance":
        return extract_vehicle_insurance_fields(text)
    if document_type_slug == "vehicle_registration":
        return extract_vehicle_registration_fields(text)
    # Future types: extractors not written yet, but PRIMARY_IDENTIFIER_KEYS
    # already records which extracted key will hold their identifier, so
    # the OCR -> DocumentForm mapping needs no change when they arrive.
    return None
