"""One-time seeding: geocode every truckstop that has no coordinates yet.

Resumable (already-geocoded rows are skipped). Lookups run on a small thread
pool while a single writer thread persists results, so the free geocoders are
still hit at a polite rate. Progress is written to a JSON snapshot at
data/stations_geocoded.json so a fresh clone can skip this step entirely:

    python manage.py geocode_stations            # run until done
    python manage.py geocode_stations --limit 50 # short test run
"""

import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.management.base import BaseCommand

from fuel.models import FuelStation
from fuel.services.geo import resolve_station
from fuel.services.trip import StationRepo

WORKERS = 8


class Command(BaseCommand):
    help = 'Geocode truckstops missing coordinates (resumable, concurrent).'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None,
                            help='Stop after this many lookups (for testing).')
        parser.add_argument('--no-export', action='store_true',
                            help='Skip writing the JSON snapshot at the end.')
        parser.add_argument('--retry-failed', action='store_true',
                            help='Reset previously failed stations to pending '
                                 'and try them again (e.g. after a rate-limit '
                                 'induced failure spike).')

    def handle(self, *args, **opts):
        if opts['retry_failed']:
            reset = FuelStation.objects.filter(
                geo_status=FuelStation.GeoStatus.FAILED).update(
                geo_status=FuelStation.GeoStatus.PENDING)
            self.stdout.write(f'Reset {reset} failed stations to pending.')
        ids = list(FuelStation.objects.filter(
            geo_status=FuelStation.GeoStatus.PENDING).values_list('id', flat=True))
        total = len(ids)
        self.stdout.write(f'{total} truckstops pending geocoding.')
        if not total:
            self._export(opts['no_export'])
            return
        if opts['limit']:
            ids = ids[:opts['limit']]

        counts = {FuelStation.GeoStatus.OK: 0,
                  FuelStation.GeoStatus.APPROX: 0,
                  FuelStation.GeoStatus.FAILED: 0}
        started = time.monotonic()

        results = queue.Queue()
        stop_event = threading.Event()

        def worker(station_id):
            if stop_event.is_set():
                return
            try:
                station = FuelStation.objects.get(pk=station_id)
                match, source, status = resolve_station(station)
            except Exception as exc:  # noqa: BLE001 -- keep the seed alive
                results.put(('error', station_id, str(exc)))
                return
            results.put(('done', station_id, (match, source, status)))

        def save_pending(counts):
            """Main thread: persist lookups as they arrive."""
            done = 0
            while done < len(ids):
                kind, station_id, payload = results.get()
                if kind == 'error':
                    done += 1
                    self.stderr.write(f'lookup {station_id} failed: {payload}')
                    continue
                match, source, status = payload
                updates = {'geo_source': source, 'geo_status': status}
                if match:
                    updates.update(lat=match['lat'], lng=match['lng'])
                FuelStation.objects.filter(pk=station_id).update(**updates)
                counts[status] = counts.get(status, 0) + 1
                done += 1
                if done % 200 == 0:
                    rate = done / max(time.monotonic() - started, 1e-6)
                    eta_min = (len(ids) - done) / max(rate, 1e-6) / 60
                    self.stdout.write(
                        f'{done}/{len(ids)} ({rate:.1f}/s, ETA {eta_min:.0f} min) '
                        f'ok={counts["ok"]} approx={counts["approx"]} '
                        f'failed={counts["failed"]}')

        saver = threading.Thread(target=save_pending, args=(counts,))
        saver.start()
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            try:
                list(pool.map(worker, ids))
            finally:
                pass  # results for all submitted tasks are queued by now
        saver.join()

        self.stdout.write(self.style.SUCCESS(
            f'Geocoding pass complete: ok={counts["ok"]} '
            f'approx={counts["approx"]} failed={counts["failed"]}'))
        self._export(opts['no_export'])

    def _export(self, skip):
        if skip:
            return
        rows = list(FuelStation.objects.exclude(lat__isnull=True).values(
            'opis_id', 'name', 'address', 'city', 'state', 'rack_id',
            'retail_price', 'lat', 'lng', 'geo_status', 'geo_source'))
        for row in rows:
            row['retail_price'] = float(row['retail_price'])
        path = settings.GEOCODED_SNAPSHOT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(rows, fh, ensure_ascii=False)
        self.stdout.write(self.style.SUCCESS(
            f'Snapshot with {len(rows)} geocoded stations written to {path}.'))
        StationRepo.invalidate()
