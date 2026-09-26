from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from projects.auth_views import register


urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="projects/login.html"),
        name="login",
    ),
    path(
        "accounts/logout/",
        auth_views.LogoutView.as_view(next_page="/"),
        name="logout",
    ),
    path("accounts/register/", register, name="register"),
    path("projects/", include("projects.urls")),
    path("catalog/", include("catalog.urls")),
    path("", include("demo.urls")),
]
