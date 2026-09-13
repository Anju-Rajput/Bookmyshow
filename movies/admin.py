from django.contrib import admin
from django.utils.html import format_html
from .models import (
    Genre, Language, CastMember, Movie, MoviePoster,
    Theater, Seat, Booking, Review,
)


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']


@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']


@admin.register(CastMember)
class CastMemberAdmin(admin.ModelAdmin):
    list_display = ['name', 'role']
    list_filter = ['role']
    search_fields = ['name']


class MoviePosterInline(admin.TabularInline):
    """Manage multiple poster images directly on the Movie page."""
    model = MoviePoster
    extra = 1


class TheaterInline(admin.TabularInline):
    """Manage show schedules for a movie directly on the Movie page."""
    model = Theater
    extra = 1


@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'language', 'age_certification', 'duration_display',
        'release_date', 'average_rating_display', 'review_count',
    ]
    list_filter = ['age_certification', 'language', 'genres', 'release_date']
    search_fields = ['name', 'description', 'cast']
    filter_horizontal = ['genres', 'cast_members']
    inlines = [MoviePosterInline, TheaterInline]
    readonly_fields = ['average_rating_display', 'trailer_preview']

    fieldsets = (
        ('Basic Info', {
            'fields': ('name', 'image', 'description', 'release_date', 'ticket_price')
        }),
        ('Classification', {
            'fields': ('genres', 'language', 'age_certification', 'duration_minutes')
        }),
        ('Cast & Crew', {
            'fields': ('cast_members', 'cast')
        }),
        ('Trailer', {
            'fields': ('trailer_url', 'trailer_preview')
        }),
        ('Ratings (auto-calculated from reviews)', {
            'fields': ('rating', 'average_rating_display')
        }),
    )

    def duration_display(self, obj):
        h, m = divmod(obj.duration_minutes or 0, 60)
        return f'{h}h {m}m' if h else f'{m}m'
    duration_display.short_description = 'Duration'

    def average_rating_display(self, obj):
        avg = obj.average_rating
        return f'{avg} / 5' if avg is not None else 'No reviews yet'
    average_rating_display.short_description = 'Average rating'

    def review_count(self, obj):
        return obj.review_count
    review_count.short_description = 'Reviews'

    def trailer_preview(self, obj):
        if obj.trailer_embed_url:
            return format_html(
                '<iframe width="320" height="180" src="{}" '
                'frameborder="0" allowfullscreen></iframe>',
                obj.trailer_embed_url
            )
        return 'No trailer set'
    trailer_preview.short_description = 'Trailer preview'


@admin.register(MoviePoster)
class MoviePosterAdmin(admin.ModelAdmin):
    list_display = ['movie', 'caption']
    list_filter = ['movie']


@admin.register(Theater)
class TheaterAdmin(admin.ModelAdmin):
    list_display = ['name', 'movie', 'city', 'time']
    list_filter = ['movie', 'city']
    search_fields = ['city']
    date_hierarchy = 'time'


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ['theater', 'seat_number', 'is_booked', 'reserved_by', 'reserved_at', 'live_status']
    list_filter = ['is_booked', 'theater']
    readonly_fields = ['reserved_by', 'reserved_at']

    def live_status(self, obj):
        return obj.status_for(obj.reserved_by) if obj.reserved_by else ('booked' if obj.is_booked else 'available')
    live_status.short_description = 'Status'


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ['user', 'seat', 'movie', 'theater', 'booked_at']
    list_filter = ['movie', 'theater']
    search_fields = ['user__username', 'movie__name']


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = [
        'movie', 'user', 'rating', 'verified_viewer',
        'report_count', 'is_hidden', 'created_at', 'updated_at',
    ]
    list_filter = ['is_hidden', 'verified_viewer', 'rating']
    search_fields = ['movie__name', 'user__username', 'comment']
    actions = ['hide_reviews', 'unhide_reviews']

    def hide_reviews(self, request, queryset):
        updated = queryset.update(is_hidden=True)
        for review in queryset:
            review.movie.update_rating_cache()
        self.message_user(request, f'{updated} review(s) hidden.')
    hide_reviews.short_description = 'Hide selected reviews (moderation)'

    def unhide_reviews(self, request, queryset):
        updated = queryset.update(is_hidden=False)
        for review in queryset:
            review.movie.update_rating_cache()
        self.message_user(request, f'{updated} review(s) unhidden.')
    unhide_reviews.short_description = 'Unhide selected reviews'
