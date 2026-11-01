# Views for the accounts app: register, login, logout, profile.
# Login and logout reuse Django's built-in views (secure and well tested).

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import redirect, render

from documents.models import Document

from .forms import ProfileEditForm


class RenewMateLoginView(LoginView):
    """Django's LoginView with our template and Bootstrap-ready form."""

    template_name = "registration/login.html"


class RenewMateLogoutView(LogoutView):
    """Django's LogoutView - logs the user out and redirects home."""

    template_name = "registration/logged_out.html"


def register(request):
    """Create a new user account and log them in straight away."""
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)  # log the new user in immediately
            messages.success(request, "Welcome to RenewMate! Your account is ready.")
            return redirect("dashboard")
        messages.error(request, "Please fix the errors below.")
    else:
        form = UserCreationForm()
    return render(request, "registration/register.html", {"form": form})


@login_required
def profile(request):
    """Simple profile page with a few account statistics."""
    documents = Document.objects.filter(owner=request.user)
    counts = {
        "total": documents.count(),
        "expired": sum(1 for d in documents if d.status() == "expired"),
        "expiring": sum(1 for d in documents if d.status() == "expiring"),
    }
    return render(request, "registration/profile.html", {"counts": counts})


@login_required
def profile_edit(request):
    """Edit the logged-in user's name, username and email."""
    if request.method == "POST":
        form = ProfileEditForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated successfully.")
            return redirect("profile")
        messages.error(request, "Please fix the errors below.")
    else:
        form = ProfileEditForm(instance=request.user)
    return render(request, "registration/profile_edit.html", {"form": form})

