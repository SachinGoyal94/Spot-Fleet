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


def build_profile(coordinates, total_distance_miles):
    """Precomputes cumulative distance along the polyline.

    Segment lengths come from a local flat-earth approximation (fine at
    routing scales), then are uniformly scaled so the polyline's total length
    matches OSRM's official route distance.
    """
    pts = np.asarray(coordinates, dtype=float)  # (N, 2) as [lng, lat]
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
    """
    if buffer_miles is None:
        buffer_miles = settings.CORRIDOR_BUFFER_MILES
    pts = profile['pts']
    cum = profile['cum']
    if len(pts) < 2 or not stations:
        return []

    slat = np.radians(pts[:-1, 1])
    slng = np.radians(pts[:-1, 0])
    elat = np.radians(pts[1:, 1])
    elng = np.radians(pts[1:, 0])

    bbox_pad_deg = (buffer_miles + 2) / 69.0
    lat_min, lat_max = pts[:, 1].min() - bbox_pad_deg, pts[:, 1].max() + bbox_pad_deg
    lng_min, lng_max = pts[:, 0].min() - bbox_pad_deg, pts[:, 0].max() + bbox_pad_deg

    kept = []
    for st in stations:
        lat, lng = st['lat'], st['lng']
        if not (lat_min <= lat <= lat_max and lng_min <= lng <= lng_max):
            continue
        miles_along, dist = _project_station(
            lat, lng, slat, slng, elat, elng, cum)
        if dist <= buffer_miles:
            kept.append({**st, 'miles_along': miles_along})
    kept.sort(key=lambda s: s['miles_along'])
    return kept


def _project_station(lat, lng, slat, slng, elat, elng, cum):
    """Nearest point on the polyline -> (miles_along, distance_to_route_miles)."""
    p_lat = np.radians(lat)
    p_lng = np.radians(lng)

    # Local flat-earth frame around the station's longitude.
    cos_lat = np.cos((slat + elat) / 2.0)
    ax = (slng - p_lng) * cos_lat
    ay = slat - p_lat
    bx = (elng - p_lng) * cos_lat
    by = elat - p_lat

    abx = bx - ax
    aby = by - ay
    ab2 = abx * abx + aby * aby
    t = np.where(ab2 > 0, -(ax * abx + ay * aby) / np.maximum(ab2, 1e-18), 0.0)
    t = np.clip(t, 0.0, 1.0)

    dx = ax + t * abx
    dy = ay + t * aby
    rad2 = dx * dx + dy * dy
    dist_miles = EARTH_RADIUS_MILES * np.sqrt(rad2)

    seg_idx = int(np.argmin(dist_miles))
    miles_along = cum[seg_idx] + t[seg_idx] * (cum[seg_idx + 1] - cum[seg_idx])
    return float(miles_along), float(dist_miles[seg_idx])


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
