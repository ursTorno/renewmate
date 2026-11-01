# Rule-based document recognition, used when a user uploads a scan on the
# Add Document page (see documents/views.py -> _handle_add_document_extract).
#
# This sits ON TOP of the existing OCR result:
#   Upload -> OCR (services/ocr.py) -> raw text -> recognize_document_type()
#
# No AI, no cloud calls, no new dependencies: just keyword scoring that a
# college student can read and explain. The result is only a SUGGESTION and
# is never written into any Document record.
"""Recognized document categories shown on the OCR page."""

# Public labels (keys are stable slugs used in the session + template).
DOCUMENT_TYPES = {
    "driving_license": "Driving License",
    "vehicle_insurance": "Vehicle Insurance",
    "vehicle_registration": "Vehicle Registration / RC",
    "passport": "Passport",
    "aadhaar": "Aadhaar",
    "pan": "PAN",
    "certificate": "Certificate",
    "other": "Unknown / Other",
}

# Each entry is (keyword, points). Matching is case-insensitive on the raw
# OCR text. Strong, document-specific phrases carry high points; generic
# words shared across documents carry low points so a single weak word can
# never decide the result on its own.
KEYWORD_SCORES = {
    "driving_license": [
        ("driving licence", 5),
        ("driving license", 5),
        ("licence to drive", 5),
        ("license to drive", 5),
        ("dl no", 3),
        ("dl number", 3),
        ("dlno", 3),
        ("valid upto", 2),
        ("date of issue", 1),
        ("transport department", 1),
    ],
    "vehicle_insurance": [
        ("motor insurance", 5),
        ("vehicle insurance", 5),
        ("policy number", 5),
        ("policy no", 4),
        ("idv", 3),
        ("premium", 3),
        ("insurance", 2),
        ("third party", 2),
        ("own damage", 2),
    ],
    "vehicle_registration": [
        ("registration certificate", 5),
        ("vehicle registration", 5),
        ("registration no", 4),
        ("chassis no", 4),
        ("engine no", 4),
        ("registration number", 3),
        ("chassis", 2),
        ("hypothecation", 2),
    ],
    "passport": [
        ("passport no", 5),
        ("passport number", 5),
        ("republic of india", 3),
        ("nationality", 3),
        ("place of issue", 2),
        ("passport", 2),
    ],
    "aadhaar": [
        ("unique identification", 5),
        ("aadhaar", 4),
        ("aadhar", 4),
        ("uidai", 4),
        ("enrolment no", 2),
        ("enrollment no", 2),
    ],
    "pan": [
        ("permanent account number", 5),
        ("income tax department", 4),
        ("income tax", 2),
        ("pan", 1),
    ],
    "certificate": [
        ("certified that", 5),
        ("certification", 4),
        ("certificate", 3),
        ("this is to certify", 4),
        ("awarded to", 2),
    ],
}

# A category must reach at least this many points to win. This stops weak,
# single-word matches (e.g. "government of india" alone) from guessing.
MIN_SCORE_TO_CLASSIFY = 4

# Short display icons (Bootstrap Icons names reused on the OCR page).
DOCUMENT_TYPE_ICONS = {
    "driving_license": "bi-person-vcard",
    "vehicle_insurance": "bi-shield-check",
    "vehicle_registration": "bi-car-front",
    "passport": "bi-airplane",
    "aadhaar": "bi-fingerprint",
    "pan": "bi-credit-card-2-front",
    "certificate": "bi-award",
    "other": "bi-file-earmark",
}


def score_document_type(text):
    """Score every category against the text. Returns {slug: points}."""
    lowered = (text or "").lower()
    scores = {}
    for slug, keywords in KEYWORD_SCORES.items():
        total = 0
        for keyword, points in keywords:
            if keyword in lowered:
                total += points
        scores[slug] = total
    return scores


def recognize_document_type(text):
    """Guess the document category from raw OCR text.

    Returns a small dict, e.g.::
        {"document_type": "driving_license",
         "label": "Driving License",
         "confidence": "High",
         "score": 11,
         "icon": "bi-person-vcard"}

    Confidence: High (>= 8 points), Medium (5-7), Low (4, or Unknown).
    Anything below MIN_SCORE_TO_CLASSIFY becomes "Unknown / Other".
    """
    if not (text or "").strip():
        return {
            "document_type": "other",
            "label": DOCUMENT_TYPES["other"],
            "confidence": "Low",
            "score": 0,
            "icon": DOCUMENT_TYPE_ICONS["other"],
        }
    scores = score_document_type(text)
    best_slug = max(scores, key=lambda slug: scores[slug])
    best_score = scores[best_slug]
    if best_score < MIN_SCORE_TO_CLASSIFY:
        return {
            "document_type": "other",
            "label": DOCUMENT_TYPES["other"],
            "confidence": "Low",
            "score": best_score,
            "icon": DOCUMENT_TYPE_ICONS["other"],
        }
    if best_score >= 8:
        confidence = "High"
    elif best_score >= 5:
        confidence = "Medium"
    else:
        confidence = "Low"
    return {
        "document_type": best_slug,
        "label": DOCUMENT_TYPES[best_slug],
        "confidence": confidence,
        "score": best_score,
        "icon": DOCUMENT_TYPE_ICONS[best_slug],
    }
