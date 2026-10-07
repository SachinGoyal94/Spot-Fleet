"""Trip orchestration: one entry point shared by the JSON API and the map page.

Per request this makes at most ONE call to the routing service (OSRM); start/
finish geocoding hits the cache or the geocoder and is persisted, and repeat
trips are served from the route cache.
"""

from django.conf import settings

from fuel.services import osrm
from fuel.services.geo import GeocodeError, geocode_query
from fuel.services.planner import (InfeasibleRoute, build_profile,
                                   corridor_stations, plan_fuel_stops)


class StationRepo:
    """In-memory snapshot of geocoded stations, refreshed on demand."""

    _loaded_at = 0.0
    _stations = []

    @classmethod
    def all_geocoded(cls):
        import time

        from fuel.models import FuelStation
        if time.monotonic() - cls._loaded_at > 30:
            rows = (FuelStation.objects
                    .exclude(lat__isnull=True)
                    .values('opis_id', 'name', 'address', 'city', 'state',
                            'retail_price', 'lat', 'lng', 'geo_status'))
            cls._stations = [
                {**r, 'retail_price': float(r['retail_price'])} for r in rows
            ]
            cls._loaded_at = time.monotonic()
        return cls._stations

    @classmethod
    def invalidate(cls):
        cls._loaded_at = 0.0


def _location_payload(text, lat, lng, display_name):
    return {'query': text, 'lat': lat, 'lng': lng, 'label': display_name}


def build_trip(start_q, finish_q):
    """Full fuel plan for a start/finish pair. Returns a JSON-ready dict."""
    start_q, finish_q = start_q.strip(), finish_q.strip()

    start_lat, start_lng, start_label = geocode_query(start_q)
    finish_lat, finish_lng, finish_label = geocode_query(finish_q)

    route = osrm.get_route(start_lat, start_lng, finish_lat, finish_lng)
    total_miles = route['distance_miles']

    profile = build_profile(route['coordinates'], total_miles)
    corridor = corridor_stations(StationRepo.all_geocoded(), profile)
    purchases, total_cost, total_gallons = plan_fuel_stops(total_miles, corridor)

    stops = []
    for i, p in enumerate(purchases, start=1):
        s = p['station']
        stops.append({
            'order': i,
            'opis_id': s['opis_id'],
            'name': s['name'],
            'address': s['address'],
            'city': s['city'],
            'state': s['state'],
            'price_per_gallon': round(s['retail_price'], 4),
            'gallons': round(p['gallons'], 2),
            'cost_usd': round(p['cost_usd'], 2),
            'lat': s['lat'],
            'lng': s['lng'],
            'miles_from_start': round(p['miles_from_start'], 1),
            'miles_since_previous': round(p['miles_since_previous'], 1),
        })

    return {
        'start': _location_payload(start_q, start_lat, start_lng, start_label),
        'finish': _location_payload(finish_q, finish_lat, finish_lng, finish_label),
        'vehicle': {
            'range_miles': settings.VEHICLE_RANGE_MILES,
            'mpg': settings.VEHICLE_MPG,
            'start_tank': 'full (already paid for)',
        },
        'total_distance_miles': round(total_miles, 1),
        'estimated_drive_hours': round(route['duration_hours'], 1),
        'total_gallons_purchased': round(total_gallons, 2),
        'total_fuel_cost_usd': round(total_cost, 2),
        'fuel_stops': stops,
        # The profile polyline is the full route decimated to a fixed spacing:
        # small enough for clients, close enough to the road for the plan.
        'route_geojson': {
            'type': 'LineString',
            'coordinates': profile['pts'].tolist(),
        },
    }
