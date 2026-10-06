"""Routing via the OSRM public demo server (free, no API key).

Exactly one HTTP call per route request; identical trips are served from the
RouteCache table.
"""

from django.conf import settings

from fuel.models import RouteCache
from fuel.services.http import get_json


class RoutingError(Exception):
    """Raised when OSRM cannot provide a driving route."""


def _cache_key(lat1, lng1, lat2, lng2):
    return f'{lat1:.5f},{lng1:.5f}|{lat2:.5f},{lng2:.5f}'


def get_route(lat1, lng1, lat2, lng2):
    """Returns {'distance_miles', 'duration_hours', 'coordinates': [[lng, lat], ...]}.

    Coordinates are the route polyline in GeoJSON order (lng first), suitable
    both for the API response and for drawing on the Leaflet map.
    """
    key = _cache_key(lat1, lng1, lat2, lng2)
    cached = RouteCache.objects.filter(key=key).first()
    if cached:
        return cached.payload

    path = f"{lng1:.5f},{lat1:.5f};{lng2:.5f},{lat2:.5f}"
    url = f'{settings.OSRM_BASE_URL}/route/v1/driving/{path}'
    params = {'overview': 'simplified', 'geometries': 'geojson', 'alternatives': 'false'}
    data = get_json(url, params=params)
    if data.get('code') != 'Ok' or not data.get('routes'):
        raise RoutingError(f'OSRM could not route between the given points '
                           f'(code: {data.get("code")})')

    route = data['routes'][0]
    payload = {
        'distance_miles': route['distance'] / 1609.344,
        'duration_hours': route['duration'] / 3600.0,
        'coordinates': route['geometry']['coordinates'],
    }
    RouteCache.objects.create(key=key, payload=payload)
    return payload
