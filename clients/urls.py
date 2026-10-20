from django.urls import path

from . import views

app_name = "clients"

urlpatterns = [
    path("", views.client_liste, name="liste"),
    path("tarifs/", views.liste_tarif_liste, name="tarifs_liste"),
    path("tarifs/nouvelle/", views.liste_tarif_form, name="tarif_nouvelle"),
    path("tarifs/<int:pk>/", views.liste_tarif_detail, name="tarif_detail"),
    path("tarifs/<int:pk>/modifier/", views.liste_tarif_form, name="tarif_modifier"),
    path("tarifs/<int:pk>/basculer/", views.liste_tarif_basculer, name="tarif_basculer"),
    path("<int:pk>/", views.client_detail, name="detail"),
]
