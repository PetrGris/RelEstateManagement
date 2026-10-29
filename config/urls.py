from django.contrib.auth import views as auth_views
from django.urls import path

from portfolio import views

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("properties/", views.properties, name="properties"),
    path("properties/<int:pk>/", views.property_detail, name="property"),
    path("edit/<str:kind>/<int:pk>/", views.edit, name="edit"),
    path("new/<str:kind>/", views.create, name="create"),
    path("leases/", views.leases, name="leases"),
    path("charges/", views.charges, name="charges"),
    path("charges/<int:pk>/", views.charge_detail, name="charge"),
    path("charges/<int:pk>/upload/", views.upload, name="upload"),
    path("documents/<int:pk>/", views.document, name="document"),
    path("export/", views.export, name="export"),
    path("correction/<str:kind>/<int:pk>/", views.correction, name="correction"),
    path("reports/", views.reports, name="reports"),
]
