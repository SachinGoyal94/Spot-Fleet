"""URL configuration for the spotfleet project (API only, no admin)."""

from django.urls import path

from fuel import views

urlpatterns = [
    path('', views.api_index, name='index'),
    path('api/route', views.route_api, name='route-api'),
    path('api/route/map', views.route_map, name='route-map'),
    path('api/health', views.health, name='health'),
]
