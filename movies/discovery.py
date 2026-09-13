"""
Movie discovery query logic - search, filters, sorting, and recommendations.

DESIGN NOTES (why it's built this way):
- Every filter is applied via .filter()/.exclude() on the QuerySet, so the
  actual row-selection happens in the database via a WHERE clause - Django
  never pulls candidate movies into Python just to check them.
- City/theater/show-date filters are combined into a SINGLE .filter() call
  when more than one is given, so they're required to match the *same*
  related Theater row (not "some theater in that city" AND, independently,
  "some theater on that date" which could be two different theaters).
- Genre and theater-relation filters are to-many joins, so .distinct() is
  applied only when such a join was actually used - avoids an unnecessary
  DISTINCT (which forces a temp sort) on the common case of no such filter.
- select_related('language') + prefetch_related('genres') avoids N+1 queries
  when the template loops over movie.language / movie.genres.all().
- Popularity sort uses Count(..., distinct=True) over the reverse Booking
  relation - computed by the DB in the same query, never by loading bookings
  into Python.
"""
from django.db.models import Count, Q

from .models import Movie, Genre, Booking, RecentlyViewed

SORT_OPTIONS = {
    'popularity': 'Most Popular',
    'newest': 'Newest Release',
    'rating': 'Highest Rated',
    'price_low': 'Price: Low to High',
    'price_high': 'Price: High to Low',
}


def search_movies(params):
    """`params` is a QueryDict (request.GET). Returns an (unpaginated,
    unevaluated) QuerySet - the caller is expected to paginate it, so the
    actual SQL LIMIT/OFFSET only fetches one page's worth of rows."""
    qs = Movie.objects.select_related('language').prefetch_related('genres')
    used_to_many_join = False

    q = (params.get('q') or '').strip()
    if q:
        qs = qs.filter(name__icontains=q)

    genre_ids = [g for g in params.getlist('genre') if g]
    if genre_ids:
        qs = qs.filter(genres__id__in=genre_ids)
        used_to_many_join = True

    language_id = params.get('language')
    if language_id:
        qs = qs.filter(language_id=language_id)

    min_rating = params.get('min_rating')
    if min_rating:
        try:
            qs = qs.filter(rating__gte=float(min_rating))
        except ValueError:
            pass

    release_from = params.get('release_from')
    if release_from:
        qs = qs.filter(release_date__gte=release_from)
    release_to = params.get('release_to')
    if release_to:
        qs = qs.filter(release_date__lte=release_to)

    # City / theater / show-date all describe the SAME showtime, so they go
    # into one combined filter() call to keep the match on a single Theater row.
    theater_conditions = {}
    city = (params.get('city') or '').strip()
    if city:
        theater_conditions['theaters__city__iexact'] = city
    theater_id = params.get('theater')
    if theater_id:
        theater_conditions['theaters__id'] = theater_id
    show_date = params.get('show_date')
    if show_date:
        theater_conditions['theaters__time__date'] = show_date
    if theater_conditions:
        qs = qs.filter(**theater_conditions)
        used_to_many_join = True

    if used_to_many_join:
        qs = qs.distinct()

    sort = params.get('sort', 'popularity')
    if sort == 'newest':
        qs = qs.order_by('-release_date', 'name')
    elif sort == 'rating':
        qs = qs.order_by('-rating', 'name')
    elif sort == 'price_low':
        qs = qs.order_by('ticket_price', 'name')
    elif sort == 'price_high':
        qs = qs.order_by('-ticket_price', 'name')
    else:  # popularity (default)
        qs = qs.annotate(popularity=Count('booking', distinct=True)).order_by('-popularity', 'name')

    return qs


def track_view(user, movie):
    """Upserts a RecentlyViewed row - cheap single UPDATE-or-INSERT, called
    from movie_detail(). Silently a no-op for anonymous users."""
    if not user.is_authenticated:
        return
    RecentlyViewed.objects.update_or_create(user=user, movie=movie)


def recommended_for_user(user, limit=8):
    """'Recommended for You': movies sharing a genre or language with what
    this user has booked or recently viewed, excluding those they've
    already seen/booked, ranked by overall popularity.

    Both signals (booking history + recently viewed) are pulled as small
    id lists first (a handful of rows each), then used to build ONE
    filtered+annotated query over Movie - never a per-movie Python loop.
    """
    if not user.is_authenticated:
        return Movie.objects.none()

    booked_movie_ids = set(Booking.objects.filter(user=user).values_list('movie_id', flat=True))
    recently_viewed_ids = set(
        RecentlyViewed.objects.filter(user=user).order_by('-viewed_at').values_list('movie_id', flat=True)[:10]
    )
    seed_movie_ids = booked_movie_ids | recently_viewed_ids
    if not seed_movie_ids:
        return Movie.objects.none()

    genre_ids = list(Genre.objects.filter(movies__id__in=seed_movie_ids).values_list('id', flat=True).distinct())
    language_ids = list(
        Movie.objects.filter(id__in=seed_movie_ids, language__isnull=False)
        .values_list('language_id', flat=True).distinct()
    )

    if not genre_ids and not language_ids:
        return Movie.objects.none()

    recs = (
        Movie.objects.filter(Q(genres__id__in=genre_ids) | Q(language_id__in=language_ids))
        .exclude(id__in=seed_movie_ids)
        .select_related('language')
        .prefetch_related('genres')
        .annotate(popularity=Count('booking', distinct=True))
        .distinct()
        .order_by('-popularity', 'name')[:limit]
    )
    return recs
