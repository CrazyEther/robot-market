from django.urls import path

from catalog import views


urlpatterns = [
    path("", views.index, name="catalog_index"),
    path("manage/", views.source_management, name="catalog_source_management"),
    path("manage/supplements/", views.supplement_management,
         name="catalog_supplement_management"),
    path("supplement/<str:checksum>/<slug:product_ref>/", views.supplement_detail,
         name="catalog_supplement_detail"),
    path("<int:family_id>/", views.family_detail, name="catalog_family_detail"),
]
