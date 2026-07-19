from django.urls import path

from . import views

app_name = "clients"

urlpatterns = [
    path("", views.client_liste, name="liste"),
    path("<int:pk>/", views.client_detail, name="detail"),
]
