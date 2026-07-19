from django.urls import path

from . import views

app_name = "caisse"

urlpatterns = [
    path("", views.liste, name="liste"),
    path("voir/", views.voir, name="voir"),
    path("cloturer/", views.cloturer, name="cloturer"),
    path("<int:pk>/", views.detail, name="detail"),
]
