"""Trip orchestration and API view tests. External services (geocoder,
OSRM) are mocked -- no network access is needed to run the suite."""

from unittest.mock import patch

from django.test import TestCase

from fuel.models import FuelStation
from fuel.services import osrm
from fuel.services.planner import InfeasibleRoute
from fuel.services.trip import StationRepo, build_trip

# A due-north route along longitude -95, 101 points from lat 39.0 to 46.27
# (~0.073 deg per point); rescaled to 600 miles by build_profile.
ROUTE = [[-95.0, 39.0 + i * 0.0727] for i in range(101)]
TOTAL_MILES = 600.0


class TripTests(TestCase):
    def _seed(self):
        """Stations hugging the route (lng -95) at various latitudes/prices."""
        def mk(id, lat, price, lng=-94.999):
            FuelStation.objects.create(
                opis_id=id, name=f'S{id}', address='I-29', city='c', state='IA',
                retail_price=price, lat=lat, lng=lng, geo_status='ok')
        mk(1, 39.10, 3.00)   # ~8 mi along
        mk(2, 39.40, 2.00)   # ~33 mi along (bargain)
        mk(3, 40.20, 2.50)   # ~99 mi along
        mk(4, 41.00, 3.50)   # ~165 mi along (expensive stretch)
        mk(5, 42.00, 2.20)   # ~248 mi along (late bargain)
        mk(6, 39.80, 1.00, lng=-94.80)  # ~11 mi OFF the route: excluded

    @patch('fuel.services.osrm.get_route')
    @patch('fuel.services.trip.geocode_query')
    def test_build_trip_plan(self, mock_geo, mock_route):
        mock_geo.side_effect = lambda q: {
            'A': (39.0, -95.0, 'Start Town'),
            'B': (46.27, -95.0, 'Finish Town'),
        }[q]
        mock_route.return_value = {
            'distance_miles': TOTAL_MILES,
            'duration_hours': 9.0,
            'coordinates': ROUTE,
        }
        self._seed()
        StationRepo.invalidate()

        trip = build_trip('A', 'B')

        self.assertEqual(trip['total_distance_miles'], 600.0)
        # Stop 6 is off-corridor and must not appear; the plan should buy at
        # the bargain (2), then use 5 (2.20) for the expensive tail.
        self.assertEqual([s['opis_id'] for s in trip['fuel_stops']], [2, 5])
        self.assertEqual(trip['fuel_stops'][0]['price_per_gallon'], 2.0)
        self.assertEqual(trip['fuel_stops'][1]['price_per_gallon'], 2.2)
        # 600 mi at 10 mpg = 60 gal total; free tank covers 50 -> 10 gal bought.
        self.assertAlmostEqual(trip['total_gallons_purchased'], 10.0, delta=0.6)
        self.assertLess(trip['total_fuel_cost_usd'], 22.0)  # all ~$2.0-2.2/gal

    @patch('fuel.services.osrm.get_route')
    @patch('fuel.services.trip.geocode_query')
    def test_short_trip_needs_no_fuel(self, mock_geo, mock_route):
        mock_geo.side_effect = lambda q: {
            'A': (39.0, -95.0, 'Start Town'),
            'B': (39.5, -95.0, 'Next Town'),
        }[q]
        mock_route.return_value = {'distance_miles': 300.0, 'duration_hours': 4.5,
                                   'coordinates': ROUTE}
        self._seed()
        StationRepo.invalidate()
        trip = build_trip('A', 'B')
        self.assertEqual(trip['fuel_stops'], [])
        self.assertEqual(trip['total_fuel_cost_usd'], 0.0)


class ViewTests(TestCase):
    CANNED_TRIP = {
        'start': {'query': 'A', 'lat': 39.0, 'lng': -95.0, 'label': 'A'},
        'finish': {'query': 'B', 'lat': 40.0, 'lng': -95.0, 'label': 'B'},
        'vehicle': {'range_miles': 500, 'mpg': 10, 'start_tank': 'full'},
        'total_distance_miles': 600.0,
        'estimated_drive_hours': 9.0,
        'total_gallons_purchased': 10.0,
        'total_fuel_cost_usd': 21.5,
        'fuel_stops': [{'order': 1, 'opis_id': 2, 'name': 'S2', 'address': 'I-29',
                        'city': 'c', 'state': 'IA', 'price_per_gallon': 2.0,
                        'gallons': 10.0, 'cost_usd': 20.0, 'lat': 39.4,
                        'lng': -95.0, 'miles_from_start': 55.0,
                        'miles_since_previous': 55.0}],
        'route_geojson': {'type': 'LineString', 'coordinates': [[-95.0, 39.0]]},
    }

    @patch('fuel.views.build_trip')
    def test_route_api_json_shape(self, mock_trip):
        mock_trip.return_value = dict(self.CANNED_TRIP)
        resp = self.client.get('/api/route?start=A&finish=B')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        for key in ('start', 'finish', 'vehicle', 'total_distance_miles',
                    'total_fuel_cost_usd', 'total_gallons_purchased',
                    'fuel_stops', 'route_geojson', 'map_url'):
            self.assertIn(key, data)
        self.assertIn('/api/route/map', data['map_url'])

    def test_route_api_requires_params(self):
        resp = self.client.get('/api/route')
        self.assertEqual(resp.status_code, 400)

    @patch('fuel.views.build_trip')
    def test_map_url_is_url_encoded(self, mock_trip):
        mock_trip.return_value = dict(self.CANNED_TRIP)
        resp = self.client.get('/api/route?start=A %26 B, TX&finish=C, TX')
        self.assertEqual(resp.status_code, 200)
        map_url = resp.json()['map_url']
        self.assertIn('start=A+%26+B%2C+TX', map_url)
        self.assertIn('finish=C%2C+TX', map_url)

    @patch('fuel.views.build_trip')
    def test_map_page_returns_422_for_infeasible_route(self, mock_trip):
        mock_trip.side_effect = InfeasibleRoute(500.0)
        resp = self.client.get('/api/route/map?start=A&finish=B')
        self.assertEqual(resp.status_code, 422)

    @patch('fuel.views.build_trip')
    def test_map_page_returns_502_for_routing_failure(self, mock_trip):
        mock_trip.side_effect = osrm.RoutingError('demo server down')
        resp = self.client.get('/api/route/map?start=A&finish=B')
        self.assertEqual(resp.status_code, 502)

    @patch('fuel.views.build_trip')
    def test_map_page_renders(self, mock_trip):
        mock_trip.return_value = dict(self.CANNED_TRIP)
        resp = self.client.get('/api/route/map?start=A&finish=B')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Total fuel cost')

    def test_health(self):
        resp = self.client.get('/api/health')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'ok')
