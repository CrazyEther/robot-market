from django.urls import path

from projects import views


urlpatterns = [
    path("", views.project_list, name="project_list"),
    path("new/<slug:slug>/", views.project_create, name="project_create"),
    path("<uuid:project_id>/", views.project_detail, name="project_detail"),
    path("<uuid:project_id>/delete/", views.project_delete, name="project_delete"),
]
