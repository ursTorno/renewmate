// Small client-side helpers for RenewMate.

document.addEventListener("DOMContentLoaded", function () {
    // Let users check what they typed into login and registration passwords.
    document.querySelectorAll("[data-password-toggle]").forEach(function (button) {
        button.addEventListener("click", function () {
            var group = button.closest(".input-group");
            var input = group && group.querySelector('input[type="password"], input[type="text"]');
            if (!input) return;

            var showPassword = input.type === "password";
            input.type = showPassword ? "text" : "password";
            button.setAttribute("aria-pressed", showPassword ? "true" : "false");
            button.setAttribute("aria-label", showPassword ? "Hide password" : "Show password");

            var icon = button.querySelector("i");
            if (icon) {
                icon.classList.toggle("bi-eye", !showPassword);
                icon.classList.toggle("bi-eye-slash", showPassword);
            }

            var accessibleText = button.querySelector(".visually-hidden");
            if (accessibleText) {
                accessibleText.textContent = showPassword ? "Hide password" : "Show password";
            }
        });
    });

    // Auto-close flash messages after 5 seconds so the screen stays clean.
    var alerts = document.querySelectorAll(".alert-dismissible");
    alerts.forEach(function (alertEl) {
        setTimeout(function () {
            var alert = bootstrap.Alert.getOrCreateInstance(alertEl);
            alert.close();
        }, 5000);
    });

    // Confirm before submitting delete forms.
    var deleteForms = document.querySelectorAll("form.confirm-delete");
    deleteForms.forEach(function (form) {
        form.addEventListener("submit", function (event) {
            if (!window.confirm("Delete this document permanently?")) {
                event.preventDefault();
            }
        });
    });

    // ================= Sidebar (off-canvas on mobile, fixed on desktop) =================
    var sidebar = document.getElementById("appSidebar");
    var backdrop = document.getElementById("sidebarBackdrop");
    var toggleBtn = document.getElementById("sidebarToggle");

    function setSidebarOpen(open) {
        if (!sidebar) return;
        sidebar.classList.toggle("open", open);
        if (backdrop) backdrop.classList.toggle("show", open);
        if (toggleBtn) toggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) {
            document.body.classList.add("sidebar-open");
        } else {
            document.body.classList.remove("sidebar-open");
        }
    }

    if (toggleBtn) {
        toggleBtn.addEventListener("click", function () {
            var isOpen = sidebar.classList.contains("open");
            setSidebarOpen(!isOpen);
        });
    }
    if (backdrop) {
        backdrop.addEventListener("click", function () {
            setSidebarOpen(false);
        });
    }
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            setSidebarOpen(false);
        }
    });
    if (sidebar) {
        // Close the off-canvas sidebar after choosing a link on mobile.
        sidebar.querySelectorAll(".sidebar-link").forEach(function (link) {
            link.addEventListener("click", function () {
                setSidebarOpen(false);
            });
        });
    }

    // ============ Add Document: run OCR as soon as a file is chosen ============
    // RenewMate is server rendered, so "automatic" OCR means the Add Document
    // page posts itself once a supported file is selected. This only happens
    // while the Title field is still empty, so nothing typed by the user is
    // ever lost (otherwise they can click "Extract Details" themselves).
    var addDocumentForm = document.querySelector("form[data-ocr-autofill]");
    if (addDocumentForm) {
        var scanInput = addDocumentForm.querySelector('input[type="file"][name="file"]');
        var titleInput = addDocumentForm.querySelector('input[name="title"]');
        var extractButton = addDocumentForm.querySelector(
            'button[name="ocr_action"][value="extract"]'
        );
        if (scanInput && extractButton) {
            scanInput.addEventListener("change", function () {
                if (!scanInput.files || scanInput.files.length === 0) return;
                if (titleInput && titleInput.value.trim() !== "") return;
                extractButton.click();
            });
        }
    }
});
