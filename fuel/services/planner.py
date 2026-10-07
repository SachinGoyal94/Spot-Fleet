"""Route-corridor matching and the fuel-stop optimization algorithm.

Corridor matching
-----------------
The route polyline from OSRM is a list of (lng, lat) points. Each truckstop
with known coordinates is projected onto the polyline: we compute the
distance from the stop to every polyline segment (vectorized with numpy) and
keep stops within ``CORRIDOR_BUFFER_MILES``. Survivors carry their "miles
along route" value, which is what the optimizer works with.

Fuel optimization
-----------------
The vehicle has a 500-mile range (50 gallons at 10 mpg) and departs with a
full, already-paid-for tank. The classic gas-station greedy:

1. While the destination is out of reach, look at every truckstop within
   reach (strictly ahead on the route).
2. Buy at the cheapest reachable one (ties: the farthest).
3. At that stop, buy just enough fuel to reach the *next equally-cheap or
   cheaper* stop within a full tank's distance; if the trip can finish on a
   full tank from there, buy exactly that amount; otherwise fill the tank
   completely (an expensive stretch lies ahead, so maximize the cheap fuel
   carried).

This is the standard capacity-constrained gas-station strategy: you never buy
expensive fuel unless forced, you never buy more of it than necessary, and
you top up at relative bargains. Cost = gallons purchased x retail price.
"""

import bisect

import numpy as np

from django.conf import settings

METERS_PER_MILE = 1609.344
EARTH_RADIUS_MILES = 3958.7613


class InfeasibleRoute(Exception):
    """A gap in the corridor longer than the vehicle's range."""

    def __init__(self, miles_from_start):
        self.miles_from_start = miles_from_start
        super().__init__(
            f'No reachable truckstop within {settings.VEHICLE_RANGE_MILES} miles '
            f'at mile {miles_from_start:.0f} of the route.'
        )


def decimate(points, min_spacing_miles):
    """Thins a polyline: keeps the endpoints and, between them, any point at
    least ``min_spacing_miles`` from the last kept point. OSRM's full geometry
    is far denser than anyone needs (tens of thousands of nodes); half a mile
    of spacing keeps the line on the road and everything downstream cheap.
    """
    pts = np.asarray(points, dtype=float)
    if len(pts) <= 2:
        return pts
    lng = np.radians(pts[:-1, 0])
    lat = np.radians(pts[:-1, 1])
    lng2 = np.radians(pts[1:, 0])
    lat2 = np.radians(pts[1:, 1])
    a = (np.sin((lat2 - lat) / 2) ** 2
         + np.cos(lat) * np.cos(lat2) * np.sin((lng2 - lng) / 2) ** 2)
    step = 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    cum = np.concatenate([[0.0], np.cumsum(step)])

    keep = [0]
    for i in range(1, len(pts) - 1):
        if cum[i] - cum[keep[-1]] >= min_spacing_miles:
            keep.append(i)
    keep.append(len(pts) - 1)
    return pts[keep]


def build_profile(coordinates, total_distance_miles):
    """Precomputes the working polyline and cumulative distance along it.

    Segment lengths come from a local flat-earth approximation (fine at
    routing scales), then are uniformly scaled so the polyline's total length
    matches OSRM's official route distance.
    """
    pts = decimate(coordinates, settings.ROUTE_MIN_SPACING_MILES)
    lng = np.radians(pts[:-1, 0])
    lat = np.radians(pts[:-1, 1])
    lng2 = np.radians(pts[1:, 0])
    lat2 = np.radians(pts[1:, 1])
    dlat = lat2 - lat
    dlng = lng2 - lng
    a = np.sin(dlat / 2) ** 2 + np.cos(lat) * np.cos(lat2) * np.sin(dlng / 2) ** 2
    seg_miles = 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    cum = np.concatenate([[0.0], np.cumsum(seg_miles)])
    if cum[-1] > 0:
        cum *= total_distance_miles / cum[-1]
    return {'pts': pts, 'cum': cum}


def corridor_stations(stations, profile, buffer_miles=None):
    """Projects stations onto the route; returns those within the corridor.

    ``stations`` is a list of dicts with at least 'lat' and 'lng'. The result
    is the same dicts (plus 'miles_along'), sorted by distance along route.

    Two vectorized passes keep this fast on full routes: a cheap screening
    pass against a heavily subsampled polyline drops everything nowhere near
    the route, then only survivors get the exact segment projection.
    """
    if buffer_miles is None:
        buffer_miles = settings.CORRIDOR_BUFFER_MILES
    pts = profile['pts']
    cum = profile['cum']
    if len(pts) < 2 or not stations:
        return []

    lat = np.radians(np.array([s['lat'] for s in stations]))[:, None]
    lng = np.radians(np.array([s['lng'] for s in stations]))[:, None]

    # Stage 1: screen against every ~50th vertex with a generous radius.
    coarse = _segments(pts[::50])
    screen = _pairwise_distances(lat, lng, *coarse).min(axis=1)
    candidates = [st for st, ok in zip(stations, screen)
                  if ok <= settings.CORRIDOR_SCREEN_MILES]
    if not candidates:
        return []

    # Stage 2: exact projection of the survivors, chunked to bound memory.
    exact = _segments(pts)
    out = []
    for i in range(0, len(candidates), 500):
        chunk = candidates[i:i + 500]
        clat = np.radians(np.array([s['lat'] for s in chunk]))[:, None]
        clng = np.radians(np.array([s['lng'] for s in chunk]))[:, None]
        dists, along = _nearest_on_route(clat, clng, exact, cum)
        for st, d, m in zip(chunk, dists, along):
            if d <= buffer_miles:
                out.append({**st, 'miles_along': m})
    out.sort(key=lambda s: s['miles_along'])
    return out


def _segments(pts):
    """Start/end radians and mid-latitude cosine for every polyline segment."""
    slat = np.radians(pts[:-1, 1])
    slng = np.radians(pts[:-1, 0])
    elat = np.radians(pts[1:, 1])
    elng = np.radians(pts[1:, 0])
    return slat, slng, elat, elng, np.cos((slat + elat) / 2.0)


def _pairwise_distances(p_lat, p_lng, slat, slng, elat, elng, cos_lat):
    """(K, M) miles from K points (radians, column vectors) to M segments."""
    ax = (slng - p_lng) * cos_lat
    ay = slat - p_lat
    abx = (elng - slng) * cos_lat
    aby = elat - slat
    t = np.clip(-(ax * abx + ay * aby) / np.maximum(abx * abx + aby * aby, 1e-18),
                0.0, 1.0)
    dx = ax + t * abx
    dy = ay + t * aby
    return EARTH_RADIUS_MILES * np.sqrt(dx * dx + dy * dy)


def _nearest_on_route(p_lat, p_lng, segments, cum):
    """Per point: (miles from route start at the projection, distance in
    miles to the route) for the closest segment."""
    dists = _pairwise_distances(p_lat, p_lng, *segments)
    best = dists.argmin(axis=1)
    rows = np.arange(len(best))
    slat, slng, elat, elng, cos_lat = segments
    ax = (slng[best] - p_lng[rows, 0]) * cos_lat[best]
    ay = slat[best] - p_lat[rows, 0]
    abx = (elng[best] - slng[best]) * cos_lat[best]
    aby = elat[best] - slat[best]
    ab2 = abx * abx + aby * aby
    t = np.where(ab2 > 0, np.clip(-(ax * abx + ay * aby) / np.maximum(ab2, 1e-18),
                                  0.0, 1.0), 0.0)
    miles = cum[best] + t * (cum[best + 1] - cum[best])
    return dists[rows, best], miles


def plan_fuel_stops(total_distance_miles, corridor, mpg=None, range_miles=None):
    """Greedy cheapest-fuel plan over corridor stops (sorted by miles_along).

    Returns a list of purchase events:
        {'station', 'miles_from_start', 'gallons', 'cost_usd',
         'miles_since_previous'}
    Raises InfeasibleRoute when a corridor gap exceeds the tank range.
    """
    if mpg is None:
        mpg = settings.VEHICLE_MPG
    if range_miles is None:
        range_miles = settings.VEHICLE_RANGE_MILES

    corridor = [s for s in corridor if 0 < s['miles_along'] < total_distance_miles]
    if not corridor:
        if total_distance_miles > range_miles:
            raise InfeasibleRoute(0.0)
        return [], 0.0, 0.0

    miles = [s['miles_along'] for s in corridor]
    pos = 0.0
    last_purchase = 0.0  # the truck starts full at mile 0
    # Depart with a full tank already paid for.
    range_left = float(range_miles)
    purchases = []
    total_cost = 0.0
    total_gallons = 0.0

    def window(frm, to):
        lo = bisect.bisect_right(miles, frm)
        hi = bisect.bisect_right(miles, to)  # inclusive: a stop exactly at
        return corridor[lo:hi]               # range's edge is reachable

    while pos + range_left < total_distance_miles:
        reachable = window(pos, pos + range_left)
        if not reachable:
            raise InfeasibleRoute(pos + range_left)

        # Buy at the cheapest reachable stop; ties go to the farthest one.
        best = min(reachable, key=lambda s: (s['retail_price'], -s['miles_along']))
        range_at_best = range_left - (best['miles_along'] - pos)
        capacity_left = range_miles - range_at_best

        # Next stop at the same or lower price within a full tank's reach?
        next_deal = [s for s in window(best['miles_along'],
                                       best['miles_along'] + range_miles)
                     if s['retail_price'] <= best['retail_price']]
        if next_deal:
            # Carry just enough of this fuel to reach the better (or equal)
            # deal instead of overbuying here.
            needed = max(0.0, next_deal[0]['miles_along']
                         - best['miles_along'] - range_at_best)
        elif total_distance_miles - best['miles_along'] <= range_miles:
            # Final leg: buy exactly enough to finish the trip.
            needed = max(0.0, total_distance_miles - best['miles_along']
                         - range_at_best)
        else:
            # Best price in reach and the stretch beyond is expensive:
            # top the tank off.
            needed = capacity_left
        needed = min(needed, capacity_left)

        if needed > 1e-9:
            gallons = needed / mpg
            cost = gallons * float(best['retail_price'])
            purchases.append({
                'station': best,
                'miles_from_start': best['miles_along'],
                'miles_since_previous': best['miles_along'] - last_purchase,
                'gallons': gallons,
                'cost_usd': cost,
            })
            total_cost += cost
            total_gallons += gallons
            last_purchase = best['miles_along']

        pos = best['miles_along']
        range_left = range_at_best + needed

    return purchases, total_cost, total_gallons
