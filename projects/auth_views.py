"""Account creation for project owners."""

from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods


def _safe_destination(request):
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return reverse("project_list")


@require_http_methods(["GET", "POST"])
def register(request):
    destination = _safe_destination(request)
    if request.user.is_authenticated:
        return redirect(destination)
    form = UserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect(destination)
    return render(request, "projects/register.html", {
        "form": form,
        "next": request.POST.get("next") or request.GET.get("next") or "",
    })
