from django.urls import path

from projects import views


urlpatterns = [
    path("", views.project_list, name="project_list"),
    path("new/<slug:slug>/", views.project_create, name="project_create"),
    path("<uuid:project_id>/", views.project_detail, name="project_detail"),
    path("<uuid:project_id>/inputs/", views.project_inputs, name="project_inputs"),
    path("<uuid:project_id>/task/", views.project_task, name="project_task"),
    path("<uuid:project_id>/robot/", views.project_robot_selection, name="project_robot_selection"),
    path("<uuid:project_id>/models/refresh/", views.project_supplement_refresh,
         name="project_supplement_refresh"),
    path("<uuid:project_id>/topology/", views.project_topology, name="project_topology"),
    path("<uuid:project_id>/sizing/", views.project_sizing, name="project_sizing"),
    path("<uuid:project_id>/operations/", views.project_operation_log, name="project_operation_log"),
    path("<uuid:project_id>/availability/", views.project_availability, name="project_availability"),
    path("<uuid:project_id>/simulation/", views.project_simulation, name="project_simulation"),
    path("<uuid:project_id>/demand/", views.project_demand_revision, name="project_demand_revision"),
    path("<uuid:project_id>/finance/", views.project_finance, name="project_finance"),
    path("<uuid:project_id>/finance/columns.csv", views.project_finance_schema,
         name="project_finance_schema"),
    path("<uuid:project_id>/finance/source.csv", views.project_finance_source,
         name="project_finance_source"),
    path("<uuid:project_id>/report.zip", views.project_report_bundle,
         name="project_report_bundle"),
    path("<uuid:project_id>/import/", views.project_import, name="project_import"),
    path("<uuid:project_id>/delete/", views.project_delete, name="project_delete"),
]
