from django.db import models


class FuelStation(models.Model):
    """A truckstop from the OPIS fuel-price list, with geocoded coordinates."""

    class GeoStatus(models.TextChoices):
        PENDING = 'pending'
        OK = 'ok'            # matched the street/exit or POI name
        APPROX = 'approx'    # fell back to the city centroid
        FAILED = 'failed'

    opis_id = models.IntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    rack_id = models.CharField(max_length=20, blank=True, default='')
    retail_price = models.DecimalField(max_digits=9, decimal_places=6)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    geo_status = models.CharField(
        max_length=10, choices=GeoStatus.choices, default=GeoStatus.PENDING
    )
    geo_source = models.CharField(max_length=20, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['geo_status'])]

    def __str__(self):
        return f'{self.name} ({self.city}, {self.state})'


class GeoCache(models.Model):
    """Cached geocoding results for start/finish queries (idempotent lookups)."""

    key = models.CharField(max_length=255, unique=True)
    lat = models.FloatField()
    lng = models.FloatField()
    display_name = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)


class RouteCache(models.Model):
    """Cached OSRM routes keyed by the coordinate pair, so repeat requests
    for the same trip skip the network entirely."""

    key = models.CharField(max_length=120, unique=True)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
