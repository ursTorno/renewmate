# Forms for the accounts app.

from django import forms
from django.contrib.auth.models import User


class ProfileEditForm(forms.ModelForm):
    """Edit the built-in User fields: name, username and email.

    Uses Django's own User model, so no extra database table is needed.
    """

    email = forms.EmailField(
        required=False,
        label="Email",
        help_text="Used for renewal email reminders. You can leave it empty.",
        widget=forms.EmailInput(
            attrs={"class": "form-control", "placeholder": "you@example.com"}
        ),
    )

    class Meta:
        model = User
        fields = ["first_name", "last_name", "username", "email"]
        widgets = {
            "first_name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Your first name"}
            ),
            "last_name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Your last name"}
            ),
            "username": forms.TextInput(attrs={"class": "form-control"}),
        }
        labels = {"first_name": "First name", "last_name": "Last name"}

    def clean_email(self):
        """One email address must not be shared by two accounts."""
        email = (self.cleaned_data.get("email") or "").strip()
        if not email:
            return email
        taken = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
        if taken.exists():
            raise forms.ValidationError(
                "This email is already used by another account."
            )
        return email

    def clean_username(self):
        """Username is the login name, so it must not be blank."""
        username = (self.cleaned_data.get("username") or "").strip()
        if not username:
            raise forms.ValidationError("Username cannot be empty.")
        return username