from django.urls import path

from demo import views


urlpatterns = [
    path("", views.index, name="index"),
    path("objects/<slug:slug>/", views.detail, name="detail"),
    path("api/v1/objects/", views.objects_api, name="objects_api"),
    path("api/v1/objects/<slug:slug>/", views.object_api, name="object_api"),
    path("health", views.health, name="health"),
    path("ready", views.ready, name="ready"),
    path("api/v1/health", views.health, name="api_health"),
    path("api/v1/ready", views.ready, name="api_ready"),
]
