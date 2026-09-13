from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard_home, name='dashboard_home'),
    path('export/<str:report>/', views.export_csv, name='dashboard_export'),
]
