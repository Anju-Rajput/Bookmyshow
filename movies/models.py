import re
from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from django.db.models import Avg
from django.utils import timezone


class Genre(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Language(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class CastMember(models.Model):
    """A person who can be attached to a movie as cast or crew."""
    ROLE_CHOICES = (
        ('actor', 'Actor'),
        ('director', 'Director'),
        ('producer', 'Producer'),
        ('writer', 'Writer'),
        ('other', 'Other'),
    )
    name = models.CharField(max_length=255)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='actor')
    photo = models.ImageField(upload_to='cast/', blank=True, null=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.get_role_display()})'


def validate_youtube_url(value):
    """Only allow youtube.com / youtu.be links so trailers embed securely."""
    pattern = r'^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)[\w-]+'
    if not re.match(pattern, value or ''):
        raise ValidationError('Enter a valid YouTube URL for the trailer.')


class AgeCertification(models.TextChoices):
    U = 'U', 'U - Universal'
    UA = 'UA', 'UA - Parental Guidance'
    A = 'A', 'A - Adults Only'
    S = 'S', 'S - Restricted to Special Class'


class Movie(models.Model):
    name = models.CharField(max_length=255)
    image = models.ImageField(upload_to="movies/")
    rating = models.DecimalField(max_digits=3, decimal_places=1, default=0)
    cast = models.TextField(blank=True, null=True, help_text="Legacy free-text cast list (optional).")
    description = models.TextField(blank=True, null=True)  # optional

    # --- Movie management fields ---
    genres = models.ManyToManyField(Genre, related_name='movies', blank=True)
    language = models.ForeignKey(Language, on_delete=models.SET_NULL, null=True, blank=True, related_name='movies')
    cast_members = models.ManyToManyField(CastMember, related_name='movies', blank=True)
    trailer_url = models.URLField(
        blank=True, null=True, validators=[validate_youtube_url],
        help_text="YouTube link, e.g. https://www.youtube.com/watch?v=XXXXXXXXXXX"
    )
    age_certification = models.CharField(
        max_length=2, choices=AgeCertification.choices, default=AgeCertification.UA
    )
    duration_minutes = models.PositiveIntegerField(default=0, help_text="Duration in minutes")
    release_date = models.DateField(null=True, blank=True)
    ticket_price = models.DecimalField(
        max_digits=8, decimal_places=2, default=200,
        help_text="Base ticket price in INR - used for price sorting/filtering in movie discovery."
    )

    class Meta:
        ordering = ['-release_date', 'name']

    def __str__(self):
        return self.name

    # --- helpers ---
    @property
    def trailer_embed_url(self):
        """Return a privacy-enhanced (youtube-nocookie) embeddable URL, or None."""
        if not self.trailer_url:
            return None
        match = re.search(
            r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([\w-]+)',
            self.trailer_url
        )
        if not match:
            return None
        video_id = match.group(1)
        return f'https://www.youtube-nocookie.com/embed/{video_id}'

    @property
    def average_rating(self):
        result = self.reviews.filter(is_hidden=False).aggregate(avg=Avg('rating'))['avg']
        return round(result, 1) if result is not None else None

    @property
    def review_count(self):
        return self.reviews.filter(is_hidden=False).count()

    def update_rating_cache(self):
        """Recompute and store the average rating on the `rating` field."""
        avg = self.average_rating
        self.rating = avg or 0
        self.save(update_fields=['rating'])

    def similar_movies(self, limit=6):
        """Movies sharing a genre or the same language, excluding this one."""
        qs = Movie.objects.exclude(id=self.id)
        genre_ids = list(self.genres.values_list('id', flat=True))
        matches = qs.filter(
            models.Q(genres__in=genre_ids) | models.Q(language=self.language)
        ).distinct()
        return matches[:limit]

    @staticmethod
    def trending(limit=6):
        """Movies with the most bookings recently, as a simple trending signal."""
        return Movie.objects.annotate(
            booking_count=models.Count('booking')
        ).order_by('-booking_count', '-release_date')[:limit]

    @staticmethod
    def recently_released(limit=6):
        return Movie.objects.filter(
            release_date__isnull=False, release_date__lte=timezone.now().date()
        ).order_by('-release_date')[:limit]


class MoviePoster(models.Model):
    """Extra poster images for a movie's gallery, beyond the main `image`."""
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='posters')
    image = models.ImageField(upload_to='movies/posters/')
    caption = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f'Poster for {self.movie.name}'


class Theater(models.Model):
    name = models.CharField(max_length=255)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='theaters')
    time = models.DateTimeField()
    city = models.CharField(max_length=100, blank=True, db_index=True, help_text="City this theater/screen is in - used for movie discovery filtering.")

    class Meta:
        indexes = [
            # Dashboard filters/sorts theaters by showtime range constantly
            # (occupancy report, "upcoming shows", etc).
            models.Index(fields=['time']),
            # Movie discovery filters by city + showtime together often.
            models.Index(fields=['city', 'time']),
        ]

    def __str__(self):
        return f'{self.name} - {self.movie.name} at {self.time}'

    def release_expired_reservations(self):
        """Free up any temporary seat holds for this theater whose 2-minute
        window has passed and payment was never completed. Wrapped in a
        transaction with row locks so this is safe to run concurrently with
        other reserve/finalize requests hitting the same seats."""
        from django.db import transaction
        cutoff = timezone.now() - timezone.timedelta(minutes=Seat.RESERVATION_MINUTES)
        with transaction.atomic():
            expired_ids = list(
                Seat.objects.select_for_update().filter(
                    theater=self,
                    is_booked=False,
                    reserved_at__isnull=False,
                    reserved_at__lt=cutoff,
                ).values_list('id', flat=True)
            )
            if expired_ids:
                Seat.objects.filter(id__in=expired_ids).update(reserved_by=None, reserved_at=None)


class Seat(models.Model):
    RESERVATION_MINUTES = 2

    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name='seats')
    seat_number = models.CharField(max_length=10)
    is_booked = models.BooleanField(default=False)

    # --- Temporary hold fields for smart seat reservation ---
    reserved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reserved_seats'
    )
    reserved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # Occupancy-per-theater aggregates GROUP BY theater with a
            # COUNT(...) FILTER (WHERE is_booked) - this composite index lets
            # the DB satisfy that with an index scan instead of scanning
            # every seat row for every theater.
            models.Index(fields=['theater', 'is_booked']),
        ]

    def __str__(self):
        return f'{self.seat_number} in {self.theater.name}'

    @property
    def reservation_expires_at(self):
        if not self.reserved_at:
            return None
        return self.reserved_at + timezone.timedelta(minutes=self.RESERVATION_MINUTES)

    @property
    def is_reservation_expired(self):
        expires_at = self.reservation_expires_at
        if expires_at is None:
            return True
        return timezone.now() >= expires_at

    @property
    def is_actively_reserved(self):
        """True if someone currently holds a live (non-expired) temporary hold."""
        return bool(self.reserved_by_id) and not self.is_reservation_expired and not self.is_booked

    def status_for(self, user):
        """Live status of this seat from the point of view of `user`."""
        if self.is_booked:
            return 'booked'
        if self.is_actively_reserved:
            if user.is_authenticated and self.reserved_by_id == user.id:
                return 'reserved_by_you'
            return 'reserved'
        return 'available'


class Booking(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    seat = models.OneToOneField(Seat, on_delete=models.CASCADE)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE)
    booked_at = models.DateTimeField(auto_now_add=True)
    payment = models.ForeignKey(
        'payments.Payment', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='bookings', help_text="The verified payment that confirmed this booking."
    )

    class Meta:
        indexes = [
            # Every trend/report query filters Booking by booked_at range,
            # often combined with a theater or movie group-by. These
            # composite indexes cover both the WHERE and the GROUP BY in one
            # index scan instead of a full-table scan + separate sort.
            models.Index(fields=['booked_at']),
            models.Index(fields=['theater', 'booked_at']),
            models.Index(fields=['movie', 'booked_at']),
        ]

    def __str__(self):
        return f'Booking by{self.user.username} for {self.seat.seat_number} at {self.theater.name}'

    @property
    def has_been_watched(self):
        """A booking counts as 'watched' once its showtime has passed."""
        return self.theater.time <= timezone.now()


class Review(models.Model):
    """A registered user's rating + review for a movie. Only allowed after a
    completed booking for that movie (see movies.views.add_review)."""
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    verified_viewer = models.BooleanField(
        default=False, help_text="True if the reviewer had a completed booking for this movie."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    reported_by = models.ManyToManyField(User, related_name='reported_reviews', blank=True)
    is_hidden = models.BooleanField(default=False, help_text="Hidden from public view by moderators.")

    class Meta:
        ordering = ['-created_at']
        unique_together = ('movie', 'user')  # one review per user per movie (editable)

    def __str__(self):
        return f'{self.user.username} rated {self.movie.name} {self.rating}/5'

    @property
    def is_edited(self):
        # allow a couple of seconds slack between create and update timestamps
        return (self.updated_at - self.created_at).total_seconds() > 2

    @property
    def report_count(self):
        return self.reported_by.count()


class RecentlyViewed(models.Model):
    """Tracks the last time each user viewed each movie's detail page.
    Feeds the 'Recommended for You' section (movie discovery feature)
    alongside booking history."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='recently_viewed')
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='viewed_by')
    viewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'movie')  # one row per user+movie, timestamp just refreshes
        indexes = [
            models.Index(fields=['user', '-viewed_at']),
        ]

    def __str__(self):
        return f'{self.user.username} viewed {self.movie.name} at {self.viewed_at}'
