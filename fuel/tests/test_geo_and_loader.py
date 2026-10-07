"""Unit tests for geocoding helpers and the CSV loader command."""

import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from fuel.models import FuelStation
from fuel.services.geo import _state_matches, in_us_bounds, parse_latlng


class GeoHelperTests(TestCase):
    def test_parse_latlng(self):
        self.assertEqual(parse_latlng('41.8781,-87.6298'), (41.8781, -87.6298))
        self.assertEqual(parse_latlng(' 39.5 ; -94.5 '), (39.5, -94.5))
        # Alaska and Hawaii are USA too.
        self.assertEqual(parse_latlng('61.2181,-149.9003'), (61.2181, -149.9003))
        self.assertEqual(parse_latlng('21.3069,-157.8583'), (21.3069, -157.8583))
        self.assertIsNone(parse_latlng('Chicago, IL'))
        self.assertIsNone(parse_latlng('91.0, 0.0'))   # out of US bounds
        self.assertIsNone(parse_latlng('41.0, 45.0'))  # out of US bounds
        self.assertIsNone(parse_latlng('51.5, -0.12')) # London is not in the US

    def test_in_us_bounds(self):
        self.assertTrue(in_us_bounds(39.0, -95.0))
        self.assertFalse(in_us_bounds(51.0, 0.0))

    def test_state_matches_accepts_code_or_full_name(self):
        self.assertTrue(_state_matches('Oklahoma', 'OK'))
        self.assertTrue(_state_matches('OK', 'OK'))
        self.assertTrue(_state_matches('ok', 'ok'))
        self.assertFalse(_state_matches('Kansas', 'OK'))
        self.assertTrue(_state_matches('', ''))
        self.assertFalse(_state_matches('', 'OK'))


class LoadStationsCommandTests(TestCase):
    CSV = (
        'OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n'
        '7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333\n'
        '7,WOODSHED DUP,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.10\n'
        '9,KWIK TRIP #796,"I-94, EXIT 143 & US-12 & SR-21",Tomah,WI,420,3.28733333\n'
    )

    def test_loads_and_dedupes_by_opis_id(self):
        with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False,
                                         encoding='utf-8') as fh:
            fh.write(self.CSV)
            path = fh.name
        try:
            call_command('load_stations', f'--csv={path}')
        finally:
            Path(path).unlink()

        self.assertEqual(FuelStation.objects.count(), 2)
        first = FuelStation.objects.get(opis_id=7)
        # Duplicate listing keeps the first occurrence.
        self.assertEqual(first.name, 'WOODSHED OF BIG CABIN')
        self.assertAlmostEqual(float(first.retail_price), 3.00733333, places=5)
        self.assertEqual(FuelStation.objects.get(opis_id=9).state, 'WI')
