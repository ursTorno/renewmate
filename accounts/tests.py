# Tests for the accounts app (registration + profile editing).
# Run them with:  python manage.py test accounts

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


def create_user(username, email=""):
    """Helper that creates a user with a known password."""
    return User.objects.create_user(
        username=username, email=email, password="secret123"
    )


class RegistrationTests(TestCase):
    """User registration flow."""

    def test_register_creates_user_and_logs_in(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "newbie",
                "password1": "something-long-123",
                "password2": "something-long-123",
            },
        )
        # After registering, the user is redirected to the dashboard.
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard/", response.url)
        user = User.objects.get(username="newbie")
        self.assertTrue(user.is_authenticated)


class ProfileEditTests(TestCase):
    """Editing the built-in User fields: name, username and email."""

    def setUp(self):
        self.user = create_user("alice", "alice@example.com")
        self.client.login(username="alice", password="secret123")

    def test_edit_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("profile_edit"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_edit_page_renders(self):
        response = self.client.get(reverse("profile_edit"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Edit profile")

    def test_update_name_and_email(self):
        response = self.client.post(
            reverse("profile_edit"),
            {
                "first_name": "Alice",
                "last_name": "Wonder",
                "username": "alice",
                "email": "alice.new@example.com",
            },
        )
        self.assertEqual(response.status_code, 302)  # back to the profile page
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Alice")
        self.assertEqual(self.user.last_name, "Wonder")
        self.assertEqual(self.user.get_full_name(), "Alice Wonder")
        self.assertEqual(self.user.email, "alice.new@example.com")

    def test_duplicate_email_rejected(self):
        create_user("bob", "bob@example.com")
        response = self.client.post(
            reverse("profile_edit"),
            {
                "first_name": "",
                "last_name": "",
                "username": "alice",
                "email": "bob@example.com",
            },
        )
        self.assertEqual(response.status_code, 200)  # form shown again with errors
        self.assertContains(response, "already used by another account")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "alice@example.com")  # unchanged

    def test_email_optional(self):
        response = self.client.post(
            reverse("profile_edit"),
            {"first_name": "", "last_name": "", "username": "alice", "email": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")

    def test_blank_username_rejected(self):
        response = self.client.post(
            reverse("profile_edit"),
            {"first_name": "", "last_name": "", "username": "", "email": "a@b.com"},
        )
        self.assertEqual(response.status_code, 200)
        # Django rejects the empty field before our custom clean() runs.
        self.assertContains(response, "This field is required.")