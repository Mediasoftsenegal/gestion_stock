from django.urls import path

from . import views

app_name = "catalogue"

urlpatterns = [
    path("articles/", views.article_liste, name="article_liste"),
    path("articles/importer/", views.article_import, name="article_import"),
    path("articles/importer/apercu/", views.article_import_preview, name="article_import_preview"),
    path("articles/<int:pk>/", views.article_detail, name="article_detail"),
    path("tarifs/nouveau/", views.tarif_form, name="tarif_nouveau"),
    path("tarifs/<int:pk>/modifier/", views.tarif_form, name="tarif_modifier"),
]
