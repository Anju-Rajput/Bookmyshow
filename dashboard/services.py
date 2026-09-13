"""
Analytics queries for the admin dashboard.

DESIGN PRINCIPLE: every function here returns an already-aggregated,
small result set (a handful of rows - one per day/theater/movie/status),
never a queryset of raw Booking/Payment/Seat rows. All summing, counting,
and grouping happens inside the database via Django's aggregation API
(Sum/Count/annotate/values), so this scales the same whether the
underlying tables have 1,000 rows or 1,000,000 - Python never sees more
than a few dozen result rows per call.
"""
from django.db.models import Sum, Count, F, Q, FloatField, ExpressionWrapper
from django.db.models.functions import TruncDate, TruncWeek, TruncMonth, TruncYear, ExtractHour

from movies.models import Booking, Theater, Movie
from payments.models import Payment
from django.contrib.auth.models import User

TRUNC_FUNCS = {
    'daily': TruncDate,
    'weekly': TruncWeek,
    'monthly': TruncMonth,
    'yearly': TruncYear,
}


def total_revenue(start, end):
    """Single SQL SUM() - O(matching rows) work done entirely in the DB,
    returns one number."""
    return Payment.objects.filter(
        status=Payment.STATUS_SUCCESS, created_at__range=(start, end)
    ).aggregate(total=Sum('amount'))['total'] or 0


def total_bookings(start, end):
    return Booking.objects.filter(booked_at__range=(start, end)).count()


def revenue_trend(start, end, granularity='daily'):
    """Revenue grouped into day/week/month/year buckets, computed via
    TruncDate/Week/Month/Year + GROUP BY in a single query."""
    trunc = TRUNC_FUNCS.get(granularity, TruncDate)
    qs = (
        Payment.objects.filter(status=Payment.STATUS_SUCCESS, created_at__range=(start, end))
        .annotate(period=trunc('created_at'))
        .values('period')
        .annotate(total=Sum('amount'), txn_count=Count('id'))
        .order_by('period')
    )
    return list(qs)


def booking_trend(start, end, granularity='daily'):
    trunc = TRUNC_FUNCS.get(granularity, TruncDate)
    qs = (
        Booking.objects.filter(booked_at__range=(start, end))
        .annotate(period=trunc('booked_at'))
        .values('period')
        .annotate(count=Count('id'))
        .order_by('period')
    )
    return list(qs)


def occupancy_by_theater(start, end):
    """Occupancy % per theater. The expensive part - counting booked vs
    total seats per theater, potentially across tens of thousands of Seat
    rows - is a single GROUP BY with a conditional COUNT, done in SQL.
    The final divide-to-percentage step runs in Python, but only over the
    handful of theater rows the aggregation already collapsed everything
    down to (never over raw seat rows)."""
    qs = (
        Theater.objects.filter(time__range=(start, end))
        .annotate(
            total_seats=Count('seats', distinct=True),
            booked_seats=Count('seats', filter=Q(seats__is_booked=True), distinct=True),
        )
        .filter(total_seats__gt=0)
        .values('id', 'name', 'movie__name', 'total_seats', 'booked_seats')
        .order_by('-booked_seats')
    )
    rows = list(qs)
    for row in rows:
        row['occupancy_pct'] = round(100.0 * row['booked_seats'] / row['total_seats'], 1)
    rows.sort(key=lambda r: r['occupancy_pct'], reverse=True)
    return rows


def most_booked_movies(start, end, limit=10):
    """Uses Count(..., filter=Q(...)) rather than .filter() + .annotate()
    on the same relation - the filter= form keeps everything in one JOIN,
    avoiding a subtle double-counting bug that a separate .filter() call on
    a to-many relation can cause."""
    qs = (
        Movie.objects.annotate(
            booking_count=Count('booking', filter=Q(booking__booked_at__range=(start, end)))
        )
        .filter(booking_count__gt=0)
        .order_by('-booking_count')
        .values('id', 'name', 'booking_count')[:limit]
    )
    return list(qs)


def top_theaters(start, end, limit=10):
    qs = (
        Theater.objects.annotate(
            revenue=Sum(
                'booking__payment__amount',
                filter=Q(booking__booked_at__range=(start, end), booking__payment__status=Payment.STATUS_SUCCESS),
            ),
            bookings=Count('booking', filter=Q(booking__booked_at__range=(start, end)), distinct=True),
        )
        .filter(bookings__gt=0)
        .order_by('-revenue')
        .values('id', 'name', 'movie__name', 'revenue', 'bookings')[:limit]
    )
    return list(qs)


def peak_booking_hours(start, end):
    """Which hour-of-day (0-23) gets the most bookings - ExtractHour lets
    the DB do the hour extraction + GROUP BY instead of pulling every
    booked_at timestamp into Python to bucket manually."""
    qs = (
        Booking.objects.filter(booked_at__range=(start, end))
        .annotate(hour=ExtractHour('booked_at'))
        .values('hour')
        .annotate(count=Count('id'))
        .order_by('hour')
    )
    return list(qs)


def cancellation_refund_stats(start, end):
    qs = (
        Payment.objects.filter(created_at__range=(start, end))
        .values('status')
        .annotate(count=Count('id'), total_amount=Sum('amount'))
        .order_by('status')
    )
    return list(qs)


def user_growth(start, end, granularity='daily'):
    trunc = TRUNC_FUNCS.get(granularity, TruncDate)
    qs = (
        User.objects.filter(date_joined__range=(start, end))
        .annotate(period=trunc('date_joined'))
        .values('period')
        .annotate(count=Count('id'))
        .order_by('period')
    )
    return list(qs)
