from django.urls import path
from . import views

urlpatterns = [
    path('theater/<int:theater_id>/pay/', views.create_payment_order, name='create_payment_order'),
    path('verify/', views.verify_payment, name='verify_payment'),
    path('<int:payment_id>/failed/', views.mark_payment_failed, name='mark_payment_failed'),
    path('webhook/razorpay/', views.razorpay_webhook, name='razorpay_webhook'),
    path('<int:payment_id>/ticket/download/', views.download_ticket, name='download_ticket'),
    path('ticket/verify/<str:booking_reference>/', views.verify_ticket, name='verify_ticket'),
]
