"""Tests for route profile building and station corridor projection."""

from django.test import TestCase

from fuel.services.planner import build_profile, corridor_stations, decimate

# A straight east-west "route" at latitude 41 spanning lng -90 .. -89
# (101 points, ~52 miles raw, rescaled by build_profile below).
ROUTE = [[-90.0 + i * 0.01, 41.0] for i in range(101)]
TOTAL_MILES = 55.0


def station(lat, lng, price=2.0, opis_id=1):
    return {'opis_id': opis_id, 'name': 'S', 'address': 'a', 'city': 'c',
            'state': 'IA', 'retail_price': price, 'lat': lat, 'lng': lng}


class CorridorTests(TestCase):
    def setUp(self):
        self.profile = build_profile(ROUTE, total_distance_miles=TOTAL_MILES)

    def test_station_on_route_is_kept_with_ordered_miles_along(self):
        near_start = station(41.0, -89.5)  # halfway along the polyline
        near_end = station(41.0, -89.1)    # 90% along the polyline
        kept = corridor_stations([near_end, near_start], self.profile,
                                 buffer_miles=2.0)
        self.assertEqual(len(kept), 2)
        self.assertLess(kept[0]['miles_along'], kept[1]['miles_along'])
        # miles_along follows the cumulative-distance fraction of the route.
        self.assertAlmostEqual(kept[0]['miles_along'], 0.5 * TOTAL_MILES, delta=1.0)
        self.assertAlmostEqual(kept[1]['miles_along'], 0.9 * TOTAL_MILES, delta=1.0)

    def test_station_far_from_route_is_dropped(self):
        far = station(41.35, -89.5)  # ~24 miles north of the route
        kept = corridor_stations([far], self.profile, buffer_miles=2.0)
        self.assertEqual(kept, [])

    def test_buffer_respected(self):
        on_edge = station(41.045, -89.5)  # ~3.1 miles off the route
        self.assertEqual(corridor_stations([on_edge], self.profile,
                                           buffer_miles=2.0), [])
        self.assertEqual(len(corridor_stations([on_edge], self.profile,
                                               buffer_miles=4.0)), 1)

    def test_profile_scales_to_osrm_distance(self):
        profile = build_profile(ROUTE, total_distance_miles=123.0)
        self.assertAlmostEqual(profile['cum'][-1], 123.0, places=6)


class DecimateTests(TestCase):
    def test_thins_dense_line_and_keeps_endpoints(self):
        # 1001 points at ~0.007 mile spacing, thinned to 0.5 miles.
        dense = [[-90.0, 41.0 + i * 0.0001] for i in range(1001)]
        thin = decimate(dense, min_spacing_miles=0.5)
        self.assertLess(len(thin), 30)
        self.assertEqual(thin[0].tolist(), dense[0])
        self.assertEqual(thin[-1].tolist(), dense[-1])
        # Every kept segment respects the spacing (small float slack), except
        # the final one: the endpoint is always kept, whatever it costs.
        for a, b in zip(thin[:-2], thin[1:-1]):
            step = abs(b[1] - a[1]) * 69.0
            self.assertGreaterEqual(step, 0.4999)

    def test_short_lines_pass_through(self):
        two = [[-90.0, 41.0], [-89.0, 41.0]]
        self.assertEqual(decimate(two, 0.5).tolist(), two)
