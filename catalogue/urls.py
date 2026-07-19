from django.urls import path

from . import views

app_name = "catalogue"

urlpatterns = [
    path("articles/", views.article_liste, name="article_liste"),
    path("articles/<int:pk>/", views.article_detail, name="article_detail"),
]
