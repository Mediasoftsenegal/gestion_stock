from rest_framework.routers import DefaultRouter

from .views import StockViewSet

app_name = "api"

router = DefaultRouter()
router.register("stock", StockViewSet, basename="stock")

urlpatterns = router.urls
