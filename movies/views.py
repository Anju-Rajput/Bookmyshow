from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import JsonResponse, HttpResponseNotAllowed
from django.utils import timezone

from .models import Movie, Theater, Seat, Booking, Review, Genre, Language
from .forms import ReviewForm
from . import discovery

PAGE_SIZE = 12


def _movie_discovery_context(request):
    """Shared by movie_list (full page) and movie_search_results (AJAX
    partial) so both stay in sync with exactly the same filtering/sorting/
    pagination logic."""
    qs = discovery.search_movies(request.GET)
    paginator = Paginator(qs, PAGE_SIZE)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    return {
        'page_obj': page_obj,
        'total_count': paginator.count,
        'genres': Genre.objects.all(),
        'languages': Language.objects.all(),
        'cities': Theater.objects.exclude(city='').values_list('city', flat=True).distinct().order_by('city'),
        'sort_options': discovery.SORT_OPTIONS,
        'current': request.GET,
    }


def movie_list(request):
    context = _movie_discovery_context(request)
    if request.user.is_authenticated:
        context['recommended_movies'] = discovery.recommended_for_user(request.user)
    return render(request, 'movies/movie_list.html', context)


def movie_search_results(request):
    """AJAX endpoint: returns just the results grid + count as an HTML
    fragment, so the filter form can update the page live without a full
    reload while still being a normal server-rendered Django view (no
    separate API/JSON contract to maintain)."""
    context = _movie_discovery_context(request)
    return render(request, 'movies/_movie_results.html', context)


def movie_detail(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)
    discovery.track_view(request.user, movie)

    reviews = movie.reviews.filter(is_hidden=False).select_related('user')

    # Has this user booked (and watched) this movie? Needed to gate review submission.
    can_review = False
    existing_review = None
    if request.user.is_authenticated:
        existing_review = Review.objects.filter(movie=movie, user=request.user).first()
        has_watched_booking = Booking.objects.filter(
            user=request.user, movie=movie, theater__time__lte=timezone.now()
        ).exists()
        can_review = has_watched_booking

    review_form = ReviewForm(instance=existing_review) if (can_review or existing_review) else None

    context = {
        'movie': movie,
        'reviews': reviews,
        'can_review': can_review,
        'existing_review': existing_review,
        'review_form': review_form,
        'similar_movies': movie.similar_movies(),
        'trending_movies': Movie.trending(),
        'recently_released': Movie.recently_released(),
    }
    return render(request, 'movies/movie_detail.html', context)


@login_required(login_url='/login/')
def add_or_edit_review(request, movie_id):
    """Registered users may submit a review only after a completed booking
    (i.e. they booked a seat for a showtime that has already happened).
    If a review already exists for this user+movie, this edits it instead."""
    movie = get_object_or_404(Movie, id=movie_id)

    has_watched_booking = Booking.objects.filter(
        user=request.user, movie=movie, theater__time__lte=timezone.now()
    ).exists()

    existing_review = Review.objects.filter(movie=movie, user=request.user).first()

    if not has_watched_booking and not existing_review:
        messages.error(request, "You can only review a movie after booking and watching it.")
        return redirect('movie_detail', movie_id=movie.id)

    if request.method == 'POST':
        form = ReviewForm(request.POST, instance=existing_review)
        if form.is_valid():
            review = form.save(commit=False)
            review.movie = movie
            review.user = request.user
            review.verified_viewer = has_watched_booking
            review.save()
            movie.update_rating_cache()
            messages.success(request, "Your review has been saved.")
            return redirect('movie_detail', movie_id=movie.id)
    else:
        form = ReviewForm(instance=existing_review)

    return render(request, 'movies/review_form.html', {'movie': movie, 'form': form})


@login_required(login_url='/login/')
def report_review(request, review_id):
    review = get_object_or_404(Review, id=review_id)
    if request.method == 'POST':
        if review.user_id == request.user.id:
            messages.error(request, "You can't report your own review.")
        elif review.reported_by.filter(id=request.user.id).exists():
            messages.info(request, "You've already reported this review.")
        else:
            review.reported_by.add(request.user)
            # simple auto-moderation threshold
            if review.report_count >= 5:
                review.is_hidden = True
                review.save(update_fields=['is_hidden'])
                review.movie.update_rating_cache()
            messages.success(request, "Thanks, this review has been reported for moderation.")
    return redirect('movie_detail', movie_id=review.movie_id)


def theater_list(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)
    theater = Theater.objects.filter(movie=movie)
    return render(request, 'movies/theater_list.html', {'movie': movie, 'theaters': theater})


@login_required(login_url='/login/')
def book_seats(request, theater_id):
    """Seat map page. Shows live availability (available / reserved-by-you /
    reserved-by-others / booked) and lets the user pick multiple seats.
    Submitting the form here just places a temporary 2-minute hold on the
    chosen seats (see reserve_seats) and sends the user on to the payment
    confirmation step - it does not book anything directly."""
    theater = get_object_or_404(Theater, id=theater_id)
    theater.release_expired_reservations()

    seats = Seat.objects.filter(theater=theater).order_by('seat_number')
    my_selected_ids = set(
        seats.filter(reserved_by=request.user).values_list('id', flat=True)
    ) if request.user.is_authenticated else set()

    seat_view = [
        {
            'obj': seat,
            'status': seat.status_for(request.user),
            'selected': seat.id in my_selected_ids,
        }
        for seat in seats
    ]

    return render(request, 'movies/seat_selection.html', {
        'theater': theater,
        'seat_view': seat_view,
        'reservation_minutes': Seat.RESERVATION_MINUTES,
    })


def seat_status_api(request, theater_id):
    """JSON endpoint the seat map polls for live availability updates."""
    theater = get_object_or_404(Theater, id=theater_id)
    theater.release_expired_reservations()
    seats = Seat.objects.filter(theater=theater).order_by('seat_number')
    data = []
    for seat in seats:
        expires_at = seat.reservation_expires_at
        data.append({
            'id': seat.id,
            'seat_number': seat.seat_number,
            'status': seat.status_for(request.user),
            'expires_in_seconds': (
                max(0, int((expires_at - timezone.now()).total_seconds()))
                if expires_at else None
            ),
        })
    return JsonResponse({'seats': data})


@login_required(login_url='/login/')
def reserve_seats(request, theater_id):
    """Place (or update) a temporary 2-minute hold on the seats the user just
    selected. Runs inside a single DB transaction with row-level locks
    (select_for_update) so two users racing for the same seat can never both
    succeed - whichever request commits first wins, the other sees the seat
    as already reserved/booked and is told to reselect."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    theater = get_object_or_404(Theater, id=theater_id)
    theater.release_expired_reservations()

    requested_ids = set(int(s) for s in request.POST.getlist('seats') if s.isdigit())

    conflict_seat_numbers = []

    with transaction.atomic():
        # Lock every seat in this theater involved in this request (either
        # currently held by the user, or newly requested) to serialize
        # concurrent reserve/finalize attempts on the same seats.
        relevant_ids = requested_ids | set(
            Seat.objects.filter(theater=theater, reserved_by=request.user).values_list('id', flat=True)
        )
        locked_seats = {
            s.id: s for s in Seat.objects.select_for_update().filter(id__in=relevant_ids, theater=theater)
        }

        # Release any seats the user previously held that are no longer selected.
        for seat in locked_seats.values():
            if seat.reserved_by_id == request.user.id and seat.id not in requested_ids:
                seat.reserved_by = None
                seat.reserved_at = None
                seat.save(update_fields=['reserved_by', 'reserved_at'])

        now = timezone.now()
        for seat_id in requested_ids:
            seat = locked_seats.get(seat_id)
            if seat is None:
                continue
            if seat.is_booked:
                conflict_seat_numbers.append(seat.seat_number)
                continue
            if seat.reserved_by_id and seat.reserved_by_id != request.user.id and not seat.is_reservation_expired:
                conflict_seat_numbers.append(seat.seat_number)
                continue
            # Free, expired, or already ours -> (re)claim it and (re)start the 2-min timer.
            seat.reserved_by = request.user
            seat.reserved_at = now
            seat.save(update_fields=['reserved_by', 'reserved_at'])

    if conflict_seat_numbers:
        messages.error(
            request,
            f"Sorry, these seats were just taken by someone else: {', '.join(conflict_seat_numbers)}. "
            "Please pick different seats."
        )
        return redirect('book_seats', theater_id=theater.id)

    if not requested_ids:
        messages.info(request, "Select at least one seat to continue.")
        return redirect('book_seats', theater_id=theater.id)

    return redirect('confirm_booking', theater_id=theater.id)


@login_required(login_url='/login/')
def confirm_booking(request, theater_id):
    """Payment confirmation step. Shows the seats currently held by this
    user with a live countdown, and lets them either proceed to pay
    (handed off to the `payments` app from here) or go back and modify
    their selection."""
    theater = get_object_or_404(Theater, id=theater_id)
    theater.release_expired_reservations()

    my_seats = Seat.objects.filter(
        theater=theater, reserved_by=request.user, is_booked=False
    ).order_by('seat_number')

    if not my_seats.exists():
        messages.info(request, "Your seat hold expired or was released. Please select seats again.")
        return redirect('book_seats', theater_id=theater.id)

    expires_at = min(seat.reservation_expires_at for seat in my_seats)
    seconds_left = max(0, int((expires_at - timezone.now()).total_seconds()))

    return render(request, 'movies/confirm_booking.html', {
        'theater': theater,
        'seats': my_seats,
        'seconds_left': seconds_left,
    })
