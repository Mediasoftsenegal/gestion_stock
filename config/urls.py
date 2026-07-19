from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core.views import dashboard

urlpatterns = [
    path("admin/", admin.site.urls),
    # Authentification (login / logout)
    path("connexion/", auth_views.LoginView.as_view(), name="login"),
    path("deconnexion/", auth_views.LogoutView.as_view(), name="logout"),
    # Tableau de bord (accueil)
    path("", dashboard, name="dashboard"),
    # Modules metier
    path("ventes/", include("ventes.urls")),
    path("stock/", include("stock.urls")),
    path("caisse/", include("caisse.urls")),
    path("clients/", include("clients.urls")),
    path("catalogue/", include("catalogue.urls")),
    # API (stock temps reel)
    path("api/", include("api.urls")),
]

admin.site.site_header = "Ets J.C.Tavarès — Gestion de stock"
admin.site.site_title = "Gestion de stock"
admin.site.index_title = "Administration"

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
