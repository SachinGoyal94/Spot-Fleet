"""Routing via the OSRM public demo server (free, no API key).

Exactly one HTTP call per route request; identical trips are served from the
RouteCache table. The demo server is best effort, so a transient failure is
retried once before the caller sees an error.
"""

import time

import requests

from django.conf import settings

from fuel.models import RouteCache
from fuel.services.http import get_json
from fuel.services.planner import decimate


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
    # Full geometry on purpose: the planner needs a line that actually hugs
    # the road for corridor matching, and trip.py thins it before responding.
    params = {'overview': 'full', 'geometries': 'geojson', 'alternatives': 'false'}

    data = None
    for attempt in range(2):
        try:
            data = get_json(url, params=params)
            break
        except (requests.RequestException, ValueError) as exc:
            if attempt == 0:
                time.sleep(0.6)
            else:
                raise RoutingError(f'Routing service unreachable: {exc}') from exc

    if not isinstance(data, dict) or data.get('code') != 'Ok' or not data.get('routes'):
        code = data.get('code') if isinstance(data, dict) else 'invalid response'
        raise RoutingError(f'OSRM could not route between the given points '
                           f'(code: {code})')

    route = data['routes'][0]
    payload = {
        'distance_miles': route['distance'] / 1609.344,
        'duration_hours': route['duration'] / 3600.0,
        # Thin the full geometry once, here, so the cache holds the working
        # polyline and repeats skip both the multi-MB read and re-decimation.
        'coordinates': decimate(
            route['geometry']['coordinates'],
            settings.ROUTE_MIN_SPACING_MILES,
        ).tolist(),
    }
    RouteCache.objects.create(key=key, payload=payload)
    return payload
