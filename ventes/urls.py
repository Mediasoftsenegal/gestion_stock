from django.urls import path

from . import views

app_name = "ventes"

urlpatterns = [
    # Commandes
    path("commandes/", views.commande_liste, name="commande_liste"),
    path("commandes/nouvelle/", views.commande_create, name="commande_create"),
    path("commandes/<int:pk>/", views.commande_detail, name="commande_detail"),
    path("commandes/<int:pk>/valider/", views.commande_valider, name="commande_valider"),
    path("commandes/<int:pk>/generer-facture/", views.commande_generer_facture, name="commande_generer_facture"),
    path("commandes/<int:pk>/supprimer/", views.commande_supprimer, name="commande_supprimer"),
    # Factures
    path("factures/", views.facture_liste, name="facture_liste"),
    path("factures/<int:pk>/", views.facture_detail, name="facture_detail"),
    path("factures/<int:pk>/pdf/", views.facture_pdf, name="facture_pdf"),
    path("factures/<int:pk>/encaisser/", views.encaisser, name="encaisser"),
    path("factures/<int:pk>/annuler/", views.facture_annuler, name="facture_annuler"),
    # Etat de suivi client
    path("suivi-client/", views.suivi_client_view, name="suivi_client"),
    path("suivi-client/annuel/", views.releve_annuel_client_view, name="releve_annuel_client"),
    # Vente au detail (comptoir)
    path("vente-detail/", views.vente_detail_create, name="vente_detail_create"),
    path("factures/<int:pk>/ticket/", views.ticket_caisse, name="ticket_caisse"),
    # AJAX
    path("ajax/tarifs/", views.ajax_tarifs, name="ajax_tarifs"),
    path("ajax/recherche-article/", views.ajax_recherche_article, name="ajax_recherche_article"),
]
