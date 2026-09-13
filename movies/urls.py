from django.urls import path
from . import views

urlpatterns = [
    path('', views.movie_list, name='movie_list'),
    path('search/results/', views.movie_search_results, name='movie_search_results'),
    path('<int:movie_id>/', views.movie_detail, name='movie_detail'),
    path('<int:movie_id>/theaters', views.theater_list, name='theater_list'),
    path('<int:movie_id>/review/', views.add_or_edit_review, name='add_or_edit_review'),
    path('review/<int:review_id>/report/', views.report_review, name='report_review'),

    # Smart seat reservation flow
    path('theater/<int:theater_id>/seats/', views.book_seats, name='book_seats'),
    path('theater/<int:theater_id>/seats/status/', views.seat_status_api, name='seat_status_api'),
    path('theater/<int:theater_id>/seats/reserve/', views.reserve_seats, name='reserve_seats'),
    path('theater/<int:theater_id>/seats/confirm/', views.confirm_booking, name='confirm_booking'),
    # Payment happens in the `payments` app from here on (see /payments/theater/<id>/pay/)
]
