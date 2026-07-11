"""web/urls.py — API + dashboard routes."""

from django.urls import path

from . import api
from .views import DashboardView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),

    path("api/products/", api.search, name="api-search"),
    path("api/products/<str:ean>/compare/", api.compare, name="api-compare"),
    path("api/products/<str:ean>/history/", api.history, name="api-history"),
    path("api/cart/optimize/", api.cart_optimize, name="api-cart-optimize"),
]
