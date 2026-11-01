# Small OCR helper reused by the Add Document page.
#
# Separation of concerns:
#   - the view (documents/views.py) handles upload + login + messages,
#   - this module only validates the image bytes and runs the OCR engine,
#   - the template (documents/document_form.html) shows the prefilled result.
# This keeps every step simple and easy to explain.

from django.conf import settings

# Image types the OCR page accepts (PDF is intentionally excluded).
OCR_ALLOWED_EXTENSIONS = ["jpg", "jpeg", "png", "webp"]

# Reasonable upper bound so one upload cannot exhaust server memory/disk.
OCR_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


class OCRError(Exception):
    """Raised when OCR cannot run or produces no usable text.

    The message is always safe to show to the user (no tracebacks leak).
    """


# Import pytesseract at module scope when it is installed. Tests patch
# ``documents.services.ocr._pytesseract`` to avoid requiring Tesseract.
try:
    import pytesseract as _pytesseract
except ImportError:  # pragma: no cover - depends on deployment
    _pytesseract = None

# Backwards-compatible alias so ``ocr.pytesseract`` also works.
pytesseract = _pytesseract


def pytesseract_available():
    """Return True when Tesseract OCR bindings are available on this server."""
    return _pytesseract is not None


class OcrService:
    """Thin service object returned by :func:`extract_text` on success."""

    def __init__(self, text):
        self.text = text
        self.character_count = len(text)
        self.line_count = text.count("\n") + 1

    def __str__(self):
        return self.text

    def __bool__(self):
        return bool(self.text)

    def html(self):
        """Safe HTML for display inside a <pre> container."""
        from django.utils.html import escape

        escaped = escape(self.text)
        return _build_html_sample(escaped)


# ----------------------------------------------------------------------
# OCR engine call (separate from upload validation and from display).
# ----------------------------------------------------------------------


def run_ocr(image):
    """Run the OCR engine against a Pillow Image and return the text.

    Raises:
        OCRError: on any engine-level problem.
    """
    if _pytesseract is None:
        raise OCRError(
            "OCR support is missing on the server. "
            "Please ask the administrator to install pytesseract."
        )
    try:
        text = _pytesseract.image_to_string(image)
    except Exception as exc:
        raise OCRError(
            "OCR could not process this image. Please try a clearer image."
        ) from exc
    text = (text or "").strip()
    if not text:
        raise OCRError("No readable text could be extracted from this image.")
    return text


# ----------------------------------------------------------------------
# Main OCR entry point
# ----------------------------------------------------------------------


def extract_text(uploaded_file):
    """Run OCR on an uploaded image and return an :class:`OcrService` result.

    Only trusted Pillow/pytesseract calls are used here: Pillow only reads
    pixels (it never executes the file), and pytesseract only passes the
    image to the local Tesseract OCR engine.

    Raises:
        OCRError: with a user-friendly message for every known problem.
    """
    name = (getattr(uploaded_file, "name", "") or "").lower()
    extension = name.rsplit(".", 1)[-1] if "." in name else ""
    if extension not in OCR_ALLOWED_EXTENSIONS:
        if not getattr(settings, "PROJECT_TESSERACT_AVAILABLE", True):
            raise OCRError(
                "OCR is not set up on this server. "
                "Please ask the administrator to install Tesseract OCR."
            )
        raise OCRError(
            "Unsupported file type. Upload a JPG, JPEG, PNG or WEBP image."
        )
    if getattr(uploaded_file, "size", 0) > OCR_MAX_UPLOAD_BYTES:
        raise OCRError("File is too large. Upload an image up to 5 MB.")

    try:
        from PIL import Image
    except ImportError:
        raise OCRError(
            "Image support is missing on the server. "
            "Please ask the administrator to install Pillow."
        )

    try:
        image = Image.open(uploaded_file)
        image.load()
    except Exception:
        raise OCRError(
            "That file does not look like a valid image. "
            "Upload a readable JPG, JPEG, PNG or WEBP image."
        )

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    text = run_ocr(image)

    return OcrService(text)


# ----------------------------------------------------------------------
# HTML output builder
# ----------------------------------------------------------------------


def _build_html_sample(text):
    """Wrap escaped OCR text in a simple, readable block."""
    return (
        "<div class=\"ocr-result\">"
        "<div class=\"ocr-meta d-flex gap-2 text-muted small mb-2\">"
        "<span>{} characters</span>"
        "<span>{} lines</span>"
        "</div>"
        "<pre class=\"ocr-text bg-light border rounded p-3 font-monospace\">{}</pre>"
        "</div>"
    ).format(
        _safe_format_count(text.count("\n") + 1 + len(text)),
        _safe_format_count(text.count("\n") + 1),
        text,
    )


def _safe_format_count(value):
    """Format a count value for the meta line. Always a plain number string."""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "0"


def extract_html(uploaded_file):
    """Convenience: run OCR and return ready-to-render HTML, or raise OCRError.

    Kept mostly for templates that prefer a single call, but the view uses
    :func:`extract_text` directly so it can show the textarea as well.
    """
    result = extract_text(uploaded_file)
    return result.html()

