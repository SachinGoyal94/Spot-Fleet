"""Unit tests for the fuel-optimization algorithm (pure logic, no network)."""

from django.test import TestCase

from fuel.services.planner import InfeasibleRoute, plan_fuel_stops


def stop(miles, price):
    return {
        'opis_id': int(miles * 10),
        'name': f'Stop @ {miles}', 'address': 'x', 'city': 'x', 'state': 'OK',
        'retail_price': price, 'lat': 0.0, 'lng': 0.0, 'miles_along': miles,
    }


class PlannerTests(TestCase):
    RANGE = 500
    MPG = 10

    def test_destination_within_single_tank_no_purchases(self):
        purchases, cost, gallons = plan_fuel_stops(
            400, [stop(100, 2.0), stop(300, 1.0)], mpg=self.MPG, range_miles=self.RANGE)
        self.assertEqual(purchases, [])
        self.assertEqual(cost, 0.0)
        self.assertEqual(gallons, 0.0)

    def test_buys_at_cheapest_reachable_and_carrys_just_enough(self):
        # Route of 1000 mi; free tank covers 500. Stops: 300@$3.00, 600@$3.50,
        # 800@$2.50. Optimal: 300 mi worth at 300 (to reach the 800 bargain),
        # then 200 mi worth at 800. -> 30 gal @$3.00 + 20 gal @$2.50 = $140.
        purchases, cost, gallons = plan_fuel_stops(
            1000, [stop(300, 3.00), stop(600, 3.50), stop(800, 2.50)],
            mpg=self.MPG, range_miles=self.RANGE)
        self.assertEqual([p['station']['miles_along'] for p in purchases], [300, 800])
        self.assertAlmostEqual(purchases[0]['gallons'], 30.0, places=6)
        self.assertAlmostEqual(purchases[1]['gallons'], 20.0, places=6)
        self.assertAlmostEqual(cost, 30 * 3.00 + 20 * 2.50, places=6)
        self.assertAlmostEqual(gallons, 50.0, places=6)

    def test_equal_price_stop_is_driven_to_not_bought_twice(self):
        # Equal-price stop at 950: buying at 450 should cover exactly the leg
        # to 950, and the final leg should buy only what the trip needs.
        purchases, cost, _ = plan_fuel_stops(
            1400, [stop(450, 2.00), stop(950, 2.00)],
            mpg=self.MPG, range_miles=self.RANGE)
        # 1400 total - 500 free = 900 mi worth = 90 gal, all at $2.00.
        self.assertEqual(len(purchases), 2)
        self.assertAlmostEqual(purchases[0]['gallons'], 45.0, places=6)
        self.assertAlmostEqual(purchases[1]['gallons'], 45.0, places=6)
        self.assertAlmostEqual(cost, 180.0, places=6)

    def test_final_leg_buys_exactly_enough(self):
        # One stop at 100 ($2.50), dest at 550: the trip needs 50 mi worth of
        # purchased fuel (550 - 500 free), all of it at 100. Arriving there
        # with 400 range, buy 50 mi worth = 5 gal.
        purchases, cost, _ = plan_fuel_stops(
            550, [stop(100, 2.50)], mpg=self.MPG, range_miles=self.RANGE)
        self.assertEqual(len(purchases), 1)
        self.assertAlmostEqual(purchases[0]['gallons'], 5.0, places=6)
        self.assertAlmostEqual(cost, 12.5, places=6)

    def test_gap_longer_than_range_is_infeasible(self):
        with self.assertRaises(InfeasibleRoute):
            plan_fuel_stops(1200, [stop(100, 2.0)], mpg=self.MPG,
                            range_miles=self.RANGE)

    def test_no_stops_and_long_route_is_infeasible(self):
        with self.assertRaises(InfeasibleRoute):
            plan_fuel_stops(600, [], mpg=self.MPG, range_miles=self.RANGE)

    def test_expensive_stretch_carries_cheap_fuel_then_buys_dear(self):
        # Cheap stop at 200 ($2.00), expensive stop at 500 ($4.00), dest at
        # 900. Optimal: top off at 200 (arrive with 300 range, capacity for
        # 200 more), arrive at 500 with 200 range, buy the last 200 mi worth
        # at $4.00 -> 20 gal @$2 + 20 gal @$4 = $120.
        purchases, cost, _ = plan_fuel_stops(
            900, [stop(200, 2.00), stop(500, 4.00)],
            mpg=self.MPG, range_miles=self.RANGE)
        self.assertEqual([p['station']['miles_along'] for p in purchases],
                         [200, 500])
        self.assertAlmostEqual(purchases[0]['gallons'], 20.0, places=6)
        self.assertAlmostEqual(purchases[1]['gallons'], 20.0, places=6)
        self.assertAlmostEqual(cost, 120.0, places=6)
