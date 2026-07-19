from django.urls import path

from . import views

app_name = "stock"

urlpatterns = [
    path("appros/", views.appro_liste, name="appro_liste"),
    path("appro/creer/", views.appro_creer, name="appro_creer"),
    path("fournisseur/<int:pk>/taxes.json", views.fournisseur_taxes_json, name="fournisseur_taxes_json"),
    path("appro/<int:pk>/editer/", views.appro_editer, name="appro_editer"),
    path("appro/<int:pk>/detail/", views.appro_detail, name="appro_detail"),
    path("appro/<int:pk>/valider/", views.appro_valider, name="appro_valider"),
    path("etat/", views.etat_stock, name="etat_stock"),
    path("sortie_liste/", views.sortie_liste, name="sortie_liste"),
    path("inventaire_liste/", views.inventaire_liste, name="inventaire_liste"),
    path("inventaire/<int:pk>/valider/", views.inventaire_valider, name="inventaire_valider"),
    path("inventaire/import/", views.inventaire_import, name="inventaire_import"),
    path("inventaire/import/apercu/", views.inventaire_import_preview, name="inventaire_import_preview"),
]
