# RenewMate

**Never miss a renewal.**

RenewMate is a personal document and renewal management web application built
with **Django** and **Bootstrap 5**. It keeps track of important documents
(ID proofs, licenses, passports, certificates...), their expiry dates and
their renewal history, and shows clearly what is valid, expiring soon or
already expired.

## Features

- User registration, login and logout, plus a profile page (Django's built-in
  auth system)
- Personal dashboard with summary cards and the documents needing attention
- Documents: add, view, edit and delete (full CRUD)
- Search, filter by document type and filter by status on the documents page
- Expiry / status tracking: **Active**, **Expiring Soon** (within 30 days) or
  **Expired**, computed live from the expiry date (documents without an expiry
  date are shown as unknown)
- Renewal history: record each renewal with its new expiry date and view the
  full history per document
- Optional document file upload (PDF / image, max 5 MB) with validation
- OCR built into the **Add Document** page (see below - fully local)
- Reminder report and reminder emails via management commands (see below)

## Project structure

    renewmate/          Django project settings (renewmate/settings.py)
    accounts/           register / login / logout / profile views
    documents/          Document model, CRUD views, expiry status logic
    documents/services/ local OCR (Tesseract), document type recognition,
                        document-specific field extraction
    dashboard/          landing page + personal dashboard
    templates/          all HTML templates (Bootstrap 5 based)
    static/             custom CSS and JS
    media/              uploaded document files (git-ignored)

## How to run (macOS / Linux)

    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py runserver

Then open <http://127.0.0.1:8000/> in a browser and register a new account.

## OCR in Add Document (fully local, no cloud API)

OCR is integrated directly into the **Add Document** page. There is no
separate scanner page and no cloud API: the text is read locally with
**Tesseract** (via `pytesseract`), and everything else - recognition and
field extraction - is rule-based Python inside this project.

Upload a document image on the Add Document page and RenewMate will:

1. read the text with Tesseract (local OCR),
2. recognise the document type with rule-based checks,
3. extract the document-specific fields,
4. prefill the Add Document form and show a review alert,
5. let you correct every field before the document is saved.

Nothing is written to the database until you click **Add Document**.

Currently supported OCR document types, and the value that is placed into
the generic **Document Number** field (each type's primary unique
identifier):

| Document type             | Primary identifier  | -> Document Number |
|---------------------------|---------------------|--------------------|
| Driving Licence           | licence number      | licence number     |
| Vehicle Insurance         | policy number       | policy number      |
| Vehicle Registration / RC | registration number | registration number|

Only these three types are extracted today. Any other document (passport,
Aadhaar, PAN, certificate...) can still be added manually: OCR failure or
partial extraction never blocks manual entry, and every extracted value
stays editable.

Supported OCR images: **JPG / JPEG / PNG / WEBP**, maximum size **5 MB**.
(An added document can also keep a PDF as its file, but PDF is not OCR'd.)

### Installing Tesseract

`pytesseract` needs the Tesseract binary on your machine:

    # macOS
    brew install tesseract
    # Debian / Ubuntu
    sudo apt install tesseract-ocr

Without it the application still works: uploading a document shows
"We couldn't extract information from this document. You can enter the
details manually."

## Reminder report

    python manage.py send_reminders

Prints every document that is expired or expires within 7 / 30 days.
The same logic can later be reused to send email or WhatsApp notifications.

## Email reminders (real sending)

Send renewal reminder emails (one per user, listing expired / due-in-30-days
documents):

    python manage.py send_email_reminders

By default this runs in *console* mode (prints emails to the terminal, sends
nothing). To send real emails with a Gmail address:

1. In Gmail: Google Account -> Security -> enable **2-Step Verification**
2. Security -> **App passwords** -> create one for "Mail" (16 characters -
   your normal Gmail password will not work for SMTP)
3. Run with your credentials as environment variables (never hardcode them):

       EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend \
       EMAIL_HOST=smtp.gmail.com \
       EMAIL_PORT=587 \
       EMAIL_USE_TLS=True \
       EMAIL_HOST_USER=you@gmail.com \
       EMAIL_HOST_PASSWORD="your-16-char-app-password" \
       DEFAULT_FROM_EMAIL="RenewMate <you@gmail.com>" \
       python manage.py send_email_reminders

To send it automatically every morning (macOS / Linux `crontab -e`):

    0 9 * * * cd "/path/to/project" && /path/to/project/venv/bin/python manage.py send_email_reminders >> /tmp/renewmate_reminders.log 2>&1

Note: users without an email address are skipped. Mobile/WhatsApp would need a
third-party service (e.g. Twilio) - not included.

The email ends with a "Log in to RenewMate" link. Its URL defaults to
`http://127.0.0.1:8000/` (the local development / demo address) and can be
changed with the `RENEWMATE_BASE_URL` environment variable, e.g.
`RENEWMATE_BASE_URL=https://renewmate.example.com`.

## Configuration (environment variables)

Deployment-sensitive values in `renewmate/settings.py` are read from
environment variables, all with safe development defaults:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DJANGO_SECRET_KEY` | development-only key | set a real secret in production |
| `DJANGO_DEBUG` | `True` | set `False` in production |
| `DJANGO_ALLOWED_HOSTS` | `127.0.0.1,localhost` | comma separated allowed hosts |
| `RENEWMATE_BASE_URL` | `http://127.0.0.1:8000` | link used in reminder emails (dev/demo) |
| `EMAIL_BACKEND` | console backend (prints to terminal) | `django.core.mail.backends.smtp.EmailBackend` to really send |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_USE_TLS` | `smtp.gmail.com` / `587` / `True` | SMTP connection |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | empty | SMTP credentials |
| `DEFAULT_FROM_EMAIL` | `RenewMate <noreply@renewmate.local>` | From address |

## Tests

    python manage.py test

## Notes

- Database: SQLite (`db.sqlite3`, created automatically by migrations).
- OCR runs locally with Tesseract - no cloud, ML or AI service is used, and
  no document data ever leaves the machine.
- Upload limits: document file and OCR images max **5 MB**; OCR accepts
  JPG / JPEG / PNG / WEBP images.
- The status of a document is never stored in the database; it is computed
  from `expiry_date`, so it is always up to date.
- Deployment-sensitive settings (`DJANGO_SECRET_KEY`, `DJANGO_DEBUG`,
  `DJANGO_ALLOWED_HOSTS`, `RENEWMATE_BASE_URL`, `EMAIL_*`) are read from
  environment variables - see *Configuration* above.
