import csv
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date

from . import services

VALID_GRANULARITIES = {'daily', 'weekly', 'monthly', 'yearly'}


def _parse_date_range(request, default_days=30):
    """Every report on this dashboard is filtered by the same start/end
    query params, so this one helper keeps that logic (and its defaults)
    in exactly one place."""
    end = parse_date(request.GET.get('end') or '') or timezone.localdate()
    start = parse_date(request.GET.get('start') or '') or (end - timedelta(days=default_days))
    if start > end:
        start, end = end, start

    start_dt = timezone.make_aware(datetime.combine(start, datetime.min.time()))
    end_dt = timezone.make_aware(datetime.combine(end, datetime.max.time()))
    return start_dt, end_dt, start, end


def _granularity(request):
    g = request.GET.get('granularity', 'daily')
    return g if g in VALID_GRANULARITIES else 'daily'


@login_required
@permission_required('dashboard.view_dashboard', raise_exception=True)
def dashboard_home(request):
    start_dt, end_dt, start, end = _parse_date_range(request)
    granularity = _granularity(request)

    context = {
        'start': start,
        'end': end,
        'granularity': granularity,
        'total_revenue': services.total_revenue(start_dt, end_dt),
        'total_bookings': services.total_bookings(start_dt, end_dt),
        'revenue_trend': services.revenue_trend(start_dt, end_dt, granularity),
        'booking_trend': services.booking_trend(start_dt, end_dt, granularity),
        'occupancy': services.occupancy_by_theater(start_dt, end_dt),
        'most_booked_movies': services.most_booked_movies(start_dt, end_dt),
        'top_theaters': services.top_theaters(start_dt, end_dt),
        'peak_hours': services.peak_booking_hours(start_dt, end_dt),
        'cancellation_stats': services.cancellation_refund_stats(start_dt, end_dt),
        'user_growth_data': services.user_growth(start_dt, end_dt, granularity),
        'can_export': request.user.has_perm('dashboard.export_dashboard_reports'),
    }
    return render(request, 'dashboard/dashboard.html', context)


# report_name -> (service function, csv column headers, needs_granularity)
REPORTS = {
    'revenue': (services.revenue_trend, ['period', 'total', 'txn_count'], True),
    'bookings': (services.booking_trend, ['period', 'count'], True),
    'occupancy': (services.occupancy_by_theater, ['name', 'movie__name', 'total_seats', 'booked_seats', 'occupancy_pct'], False),
    'movies': (services.most_booked_movies, ['id', 'name', 'booking_count'], False),
    'theaters': (services.top_theaters, ['id', 'name', 'movie__name', 'revenue', 'bookings'], False),
    'peak_hours': (services.peak_booking_hours, ['hour', 'count'], False),
    'cancellations': (services.cancellation_refund_stats, ['status', 'count', 'total_amount'], False),
    'user_growth': (services.user_growth, ['period', 'count'], True),
}


@login_required
@permission_required('dashboard.export_dashboard_reports', raise_exception=True)
def export_csv(request, report):
    if report not in REPORTS:
        return HttpResponseBadRequest('Unknown report type.')

    start_dt, end_dt, start, end = _parse_date_range(request)
    func, fields, needs_granularity = REPORTS[report]

    rows = func(start_dt, end_dt, _granularity(request)) if needs_granularity else func(start_dt, end_dt)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{report}_{start}_{end}.csv"'
    writer = csv.writer(response)
    writer.writerow(fields)
    for row in rows:
        writer.writerow([row.get(f, '') for f in fields])
    return response
