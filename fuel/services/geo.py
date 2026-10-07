"""Geocoding on top of free OpenStreetMap services (no API keys).

Two entry points:

* ``geocode_query``          -- free-text start/finish lookups ("Chicago, IL",
  "60601", "41.85,-87.65"), cached in the GeoCache table.
* ``geocode_station``        -- the truckstop list. Truckstop addresses are
  highway-exit descriptions such as "I-44, EXIT 283 & US-69", so a chain of
  strategies is tried and the first result that lands in the expected state
  wins. This runs once during seeding (see geocode_stations command) and the
  coordinates are persisted, so the request path never geocodes stations.
"""

import re
import threading
import time

from django.conf import settings
from django.db import transaction

from fuel.models import FuelStation, GeoCache
from fuel.services.http import RateLimiter, get_json

from .http import SESSION

_photon_limiter = RateLimiter(settings.PHOTON_QPS)
_nominatim_limiter = RateLimiter(settings.NOMINATIM_QPS)

# Identical queries (e.g. the city-centroid fallback for several truckstops
# in the same town) hit Photon once and are served from this cache.
_photon_cache: dict = {}
_photon_cache_lock = threading.Lock()

STATE_NAMES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota',
    'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska',
    'NV': 'Nevada', 'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico',
    'NY': 'New York', 'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio',
    'OK': 'Oklahoma', 'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island',
    'SC': 'South Carolina', 'SD': 'South Dakota', 'TN': 'Tennessee',
    'TX': 'Texas', 'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia',
    'WA': 'Washington', 'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
    'DC': 'District of Columbia',
}

# Rough bounding box of the US including Alaska and Hawaii. Anything outside
# is a geocoder mismatch.
US_BOUNDS = {'lat_min': 18.0, 'lat_max': 72.0, 'lng_min': -180.0, 'lng_max': -66.5}

_LATLNG_RE = re.compile(r'^\s*(-?\d{1,2}(?:\.\d+)?)\s*[,; ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$')


class GeocodeError(Exception):
    """Raised when a location cannot be resolved to US coordinates."""


def in_us_bounds(lat, lng):
    return (
        US_BOUNDS['lat_min'] <= lat <= US_BOUNDS['lat_max']
        and US_BOUNDS['lng_min'] <= lng <= US_BOUNDS['lng_max']
    )


def parse_latlng(text):
    """Accept explicit "lat,lng" input (e.g. "41.8781,-87.6298")."""
    m = _LATLNG_RE.match(text)
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if not in_us_bounds(lat, lng):
        return None
    return lat, lng


def _photon(params):
    q = params.get('q', '')
    with _photon_cache_lock:
        cached = _photon_cache.get(q)
    if cached is not None:
        yield from cached
        return

    _photon_limiter.wait()
    data = get_json(settings.PHOTON_BASE_URL, params=params)
    candidates = []
    for feature in data.get('features', []):
        props = feature.get('properties', {})
        lon, lat = feature['geometry']['coordinates']
        candidates.append({
            'lat': lat, 'lng': lon,
            'state': (props.get('state') or '').strip(),
            'city': (props.get('city') or props.get('county') or '').strip(),
            'name': props.get('name') or '',
            'label': ', '.join(filter(None, [
                props.get('name'), props.get('street'),
                props.get('city'), props.get('state'),
            ])),
        })
    with _photon_cache_lock:
        _photon_cache[q] = candidates
    yield from candidates


def _state_from_display(display_name):
    """Pull the state name out of a Nominatim display string. Longest match
    first, so 'West Virginia' is never read as 'Virginia'."""
    for name in sorted(STATE_NAMES.values(), key=len, reverse=True):
        if name in display_name:
            return name
    return ''


def _nominatim(params):
    _nominatim_limiter.wait()
    params = {'format': 'jsonv2', 'limit': 3, 'countrycodes': 'us', **params}
    data = get_json(settings.NOMINATIM_BASE_URL, params=params)
    for row in data:
        display = row.get('display_name', '')
        yield {
            'lat': float(row['lat']), 'lng': float(row['lon']),
            'state': _state_from_display(display),
            'city': '',
            'name': row.get('name') or '',
            'label': display.split(',')[0] if display else '',
        }


def _state_matches(candidate_state, expected_state):
    """Candidate state may be a code ('OK', Nominatim) or a full name
    ('Oklahoma', Photon); the CSV always uses codes."""
    if not expected_state:
        return True
    expected = expected_state.upper()
    return candidate_state.upper() in (expected, STATE_NAMES.get(expected, '').upper())


def _pick(candidates, expected_state):
    """First candidate inside the US (and in the expected state, if known)."""
    for cand in candidates:
        if not in_us_bounds(cand['lat'], cand['lng']):
            continue
        if _state_matches(cand['state'], expected_state):
            return cand
    return None


def resolve_station(station):
    """Strategy chain for one FuelStation. Returns (result|None, source, status).

    Pure lookup + validation; persistence is up to the caller (the seeding
    command parallelizes lookups and saves from a single writer thread).
    """
    address = station.address.replace('&', ' ').strip()
    name = station.name.strip()
    city_state = f"{station.city}, {station.state}"

    strategies = [
        ('photon:address', lambda: _photon({'q': f'{address}, {city_state}', 'limit': 5})),
        ('photon:name', lambda: _photon({'q': f'{name}, {city_state}', 'limit': 5})),
        ('nominatim:address', lambda: _nominatim({'q': f'{address}, {city_state}, USA'})),
        ('nominatim:name', lambda: _nominatim({'q': f'{name}, {city_state}, USA'})),
    ]
    for source, fetch in strategies:
        try:
            match = _pick(fetch(), station.state)
        except Exception:
            time.sleep(1.0)
            continue
        if match:
            return match, source, FuelStation.GeoStatus.OK

    # Last resort: city centroid. Still useful for corridor matching when the
    # city is small (most truckstop towns are), flagged as approximate.
    try:
        match = _pick(_photon({'q': city_state, 'limit': 1}), station.state)
        if match:
            return match, 'photon:city', FuelStation.GeoStatus.APPROX
    except Exception:
        pass
    return None, '', FuelStation.GeoStatus.FAILED


def geocode_station(station):
    """Geocode one FuelStation instance and persist the result."""
    match, source, status = resolve_station(station)
    station.geo_source = source
    station.geo_status = status
    if match:
        station.lat, station.lng = match['lat'], match['lng']
    with transaction.atomic():
        station.save(update_fields=[
            'lat', 'lng', 'geo_status', 'geo_source', 'updated_at',
        ])
    return status


def geocode_query(text):
    """Resolve a free-text place to (lat, lng, display_name). Cached."""
    text = text.strip()
    direct = parse_latlng(text)
    if direct:
        lat, lng = direct
        return lat, lng, f'{lat:.5f}, {lng:.5f}'

    key = re.sub(r'\s+', ' ', text.lower())
    cached = GeoCache.objects.filter(key=key).first()
    if cached:
        return cached.lat, cached.lng, cached.display_name

    attempts = [
        lambda: _nominatim({'q': f'{text}, USA'}),
        lambda: _photon({'q': text, 'limit': 5}),
        lambda: _nominatim({'q': text}),
    ]
    for fetch in attempts:
        try:
            match = _pick(fetch(), None)
        except Exception:
            continue
        if match:
            GeoCache.objects.create(
                key=key, lat=match['lat'], lng=match['lng'],
                display_name=match['label'] or text,
            )
            return match['lat'], match['lng'], match['label'] or text

    raise GeocodeError(f"Could not geocode location: '{text}'")
