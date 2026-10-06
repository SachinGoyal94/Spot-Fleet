"""Load the OPIS truckstop price list into FuelStation rows.

Usage:
    python manage.py load_stations                     # from the CSV
    python manage.py load_stations --from-json data/stations_geocoded.json
"""

import csv
import json
import sys

from django.core.management.base import BaseCommand
from django.db import transaction

from fuel.models import FuelStation
from fuel.services.trip import StationRepo


class Command(BaseCommand):
    help = 'Load truckstop fuel prices into the database.'

    def add_arguments(self, parser):
        parser.add_argument('--csv', help='Path to the OPIS price CSV')
        parser.add_argument('--from-json', dest='from_json',
                            help='Path to a geocoded snapshot JSON instead of the CSV')

    def handle(self, *args, **opts):
        if opts['from_json']:
            self._load_json(opts['from_json'])
        else:
            self._load_csv(opts['csv'])

    def _load_csv(self, csv_path):
        import pathlib
        if not csv_path:
            candidates = list(pathlib.Path('.').glob('*fuel*prices*.csv'))
            if not candidates:
                self.stderr.write('No CSV given and none matching '
                                  '*fuel*prices*.csv found in the project root.')
                sys.exit(1)
            csv_path = str(candidates[0])

        seen = {}
        with open(csv_path, newline='', encoding='utf-8-sig') as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                opis_id = int(row['OPIS Truckstop ID'])
                if opis_id in seen:
                    continue  # duplicate listings keep the first occurrence
                seen[opis_id] = {
                    'opis_id': opis_id,
                    'name': row['Truckstop Name'].strip(),
                    'address': row['Address'].strip(),
                    'city': row['City'].strip(),
                    'state': row['State'].strip().upper(),
                    'rack_id': (row.get('Rack ID') or '').strip(),
                    'retail_price': row['Retail Price'].strip(),
                }

        with transaction.atomic():
            for data in seen.values():
                FuelStation.objects.update_or_create(
                    opis_id=data['opis_id'], defaults=data)
        self.stdout.write(self.style.SUCCESS(
            f'Loaded {len(seen)} unique truckstops from {csv_path} '
            f'({FuelStation.objects.count()} total in DB).'))

    def _load_json(self, json_path):
        with open(json_path, encoding='utf-8') as fh:
            rows = json.load(fh)
        with transaction.atomic():
            for row in rows:
                FuelStation.objects.update_or_create(
                    opis_id=row['opis_id'], defaults=row)
        self.stdout.write(self.style.SUCCESS(
            f'Loaded {len(rows)} truckstops (with coordinates) from {json_path}.'))

        StationRepo.invalidate()
