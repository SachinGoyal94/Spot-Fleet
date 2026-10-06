# Spot Fleet — Fuel-Optimized Route API

A Django REST-style API that plans the cheapest fueling strategy for a truck
trip between two US locations.

Give it a start and a finish (free text or `lat,lng`) and it returns:

- the **route** as GeoJSON (drawable directly on a map),
- the **optimal fuel stops** along the route with per-stop gallons & cost,
- the **total money spent on fuel** (10 mpg, 500-mile max range),
- and an **interactive map page** with the route and numbered fuel-stop pins.

Every external service is free and key-less — no API keys, no signup:

| Concern | Service |
| --- | --- |
| Driving route | [OSRM](https://project-osrm.org/) public demo server (OpenStreetMap data) |
| Geocoding | [Photon](https://photon.komoot.io/) by komoot, [Nominatim](https://nominatim.openstreetmap.org/) as fallback |
| Map tiles | CARTO raster tiles on Leaflet (OpenStreetMap data) |

## Quick start

```bash
# Windows (repo ships a ready .venv; recreate with `python -m venv .venv` if needed)
.venv\Scripts\activate
pip install -r requirements.txt

python manage.py migrate
python manage.py load_stations --from-json data/stations_geocoded.json   # instant
#   ...or from the raw CSV (geocodes 6,7k truckstops once, ~45 min):
#   python manage.py load_stations && python manage.py geocode_stations

python manage.py runserver
```

Then open:

- `http://127.0.0.1:8000/api/route?start=Cincinnati, OH&finish=Springfield, MO`
- `http://127.0.0.1:8000/api/route/map?start=Cincinnati, OH&finish=Springfield, MO`

A ready-made Postman collection is in [`postman_collection.json`](postman_collection.json).

## API

### `GET /api/route?start=<place>&finish=<place>`

```json
{
  "start": {"query": "Cincinnati, OH", "lat": 39.103, "lng": -84.512, "label": "Cincinnati"},
  "finish": {"query": "Springfield, MO", "lat": 37.209, "lng": -93.292, "label": "Springfield"},
  "vehicle": {"range_miles": 500, "mpg": 10, "start_tank": "full (already paid for)"},
  "total_distance_miles": 611.4,
  "estimated_drive_hours": 9.0,
  "total_gallons_purchased": 11.1,
  "total_fuel_cost_usd": 34.02,
  "fuel_stops": [
    {
      "order": 1, "opis_id": 2745, "name": "SAPP BROS. PETROL", "address": "I-70, EXIT 166...",
      "city": "Boonville", "state": "MO", "price_per_gallon": 2.96,
      "gallons": 11.1, "cost_usd": 32.86, "lat": 38.95, "lng": -92.75,
      "miles_from_start": 455.2, "miles_since_previous": 455.2
    }
  ],
  "route_geojson": {"type": "LineString", "coordinates": [[-84.51, 39.1], ...]},
  "map_url": "http://127.0.0.1:8000/api/route/map?start=Cincinnati, OH&finish=Springfield, MO"
}
```

`start`/`finish` accept free text ("Cincinnati, OH", a ZIP) or explicit
`lat,lng`. Status codes: `200` ok · `400` missing params · `404` location not
resolvable · `422` route infeasible (a >500 mi gap between truckstops) ·
`502` routing service error.

Other endpoints: `GET /api/route/map` (HTML map) · `GET /api/health` · `GET /`.

## How it works

```
GET /api/route?start=...&finish=...
   │
   ├─ geocode start/finish        Photon → Nominatim, cached in DB
   ├─ ONE call to OSRM            route geometry + distance (cached per pair)
   ├─ corridor filter             project the 6,7k truckstops onto the route
   │                              polyline (numpy), keep those ≤ 4 mi away
   ├─ fuel optimizer              greedy "gas station problem" plan
   └─ respond                     JSON (or render the Leaflet map page)
```

### The fuel optimizer

Assumptions (all configurable in `spotfleet/settings.py`): 500-mile max range
on a full tank, 10 mpg, the truck departs with a **full, already-paid-for**
tank, prices are the provided retail snapshot, and only truckstops within
`CORRIDOR_BUFFER_MILES` (4 mi) of the route are considered.

The planner is the classic capacity-constrained gas-station greedy:

1. While the destination is out of reach, look at every truckstop reachable
   with the remaining fuel (strictly ahead on the route).
2. Buy at the **cheapest** reachable one (ties: the farthest).
3. There, buy just enough to reach the **next equally-cheap or cheaper**
   stop within a full tank's distance; if the trip can finish on one tank,
   buy exactly that amount; otherwise fill the tank completely (an expensive
   stretch lies ahead, so maximize cheap fuel carried).

This never buys expensive fuel unless forced, never overbuys it, and tops up
at bargains. Gallons × price per stop are summed into `total_fuel_cost_usd`.

### Speed

Per request: 1 geocoder lookup per uncached endpoint + **exactly one OSRM
call**; everything else is local (numpy projection is a few ms). Start/finish
geocodes and OSRM routes are persisted in caches (`GeoCache`, `RouteCache`),
so repeat and similar trips answer from SQLite. A fresh cross-country query
takes ~1–2 s end-to-end; cached ones answer in tens of milliseconds.

### Why no API keys

OSRM's demo server, Photon, Nominatim and CARTO's public tiles all serve
anonymous traffic under fair-use policies. The heavy part — geocoding 6,738
truckstop addresses — is done **once** during seeding and committed as
`data/stations_geocoded.json`, so neither the API nor a fresh clone ever
needs it again.

## Project layout

```
spotfleet/            Django project (settings, root urls)
fuel/
  models.py           FuelStation, GeoCache, RouteCache
  views.py            /api/route, /api/route/map, /api/health
  services/
    geo.py            Photon→Nominatim geocoding chain + caches
    osrm.py           OSRM client (1 call/request) + route cache
    planner.py        corridor projection (numpy) + fuel optimizer
    trip.py           orchestration shared by JSON and map views
    http.py           session, UA header, rate limiter
  management/commands/
    load_stations.py  CSV → DB (dedupes by OPIS ID)
    geocode_stations.py  one-time concurrent geocoding seed + snapshot
  templates/fuel/     Leaflet map page
  tests/              21 unit tests (no network needed)
data/
  stations_geocoded.json   seeded coordinates snapshot
```

## Testing

```bash
python manage.py test fuel.tests
```

Covers the optimizer (cost-optimal plans on synthetic routes), the corridor
projection math, the geocoding helpers, the CSV loader (dedup) and the API
views (external services mocked).

## Assumptions & notes

- Both endpoints must be in the USA (geocoders are restricted to US results).
- Fuel purchased before departure (starting tank) is not part of the trip cost.
- Truckstop coordinates come from OSM geocoding of the provided addresses;
  city-centroid fallbacks are flagged `approx` in the data snapshot.
- OSRM's demo server is rate-limited and best-effort; for production you'd
  self-host it (Docker) behind the same client.
