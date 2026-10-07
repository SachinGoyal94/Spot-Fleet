# Spot Fleet: Fuel-Optimized Route API

> **Demo video:** https://www.tella.tv/video/fuel-optimized-routing-api-submission-1u71

Django API that plans the cheapest way to fuel a truck trip between two US
locations. You give it a start and a finish (free text like "Cincinnati, OH"
or explicit lat,lng) and it returns:

- the route as GeoJSON, ready to draw on a map,
- the fuel stops to buy at, with price, gallons and cost per stop,
- the total money spent on fuel,
- and a map page with the route and the stops drawn on it.

The truck does 10 mpg with a 500 mile range on a full tank. Everything the
API calls is free and needs no keys: OSRM (public demo server) for routing,
Photon and Nominatim for geocoding, and plain OpenStreetMap tiles for the
map. There is no signup or credential anywhere in the project.

## Running it

The repo ships a ready `.venv` (Python 3.13). On Windows:

```
.venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py load_stations --from-json data/stations_geocoded.json
python manage.py runserver
```

`load_stations --from-json` loads the 6,627 geocoded truckstops from the
committed snapshot and takes a few seconds. If you want to rebuild that
snapshot from the raw OPIS price list instead (the CSV is not committed,
drop it in the repo root first), run:

```
python manage.py load_stations
python manage.py geocode_stations
```

The second command geocodes the whole list, which takes about an hour, so it
is a one-time seeding step and normally nobody needs to run it.

Settings ship with `DEBUG=True` and `ALLOWED_HOSTS=['*']` because this is a
local demo; change both before running it anywhere public.

Then open:

- `http://127.0.0.1:8000/api/route?start=Cincinnati, OH&finish=Springfield, MO`
- `http://127.0.0.1:8000/api/route/map?start=Cincinnati, OH&finish=Springfield, MO`

There is also a Postman collection in `postman_collection.json` with a few
requests I used for testing.

## The API

### `GET /api/route?start=<place>&finish=<place>`

```json
{
  "start": {"query": "Cincinnati, OH", "lat": 39.103, "lng": -84.512, "label": "Cincinnati"},
  "finish": {"query": "Springfield, MO", "lat": 37.209, "lng": -93.292, "label": "Springfield"},
  "vehicle": {"range_miles": 500, "mpg": 10, "start_tank": "full (already paid for)"},
  "total_distance_miles": 563.9,
  "estimated_drive_hours": 10.6,
  "total_gallons_purchased": 6.39,
  "total_fuel_cost_usd": 18.98,
  "fuel_stops": [
    {
      "order": 1, "opis_id": 6683, "name": "STUCKEYS TRAVEL PLAZA", "address": "I-44, EXIT ...",
      "city": "Lebanon", "state": "MO", "price_per_gallon": 2.9723,
      "gallons": 6.32, "cost_usd": 18.78, "lat": 37.68, "lng": -92.66,
      "miles_from_start": 462.0, "miles_since_previous": 462.0
    }
  ],
  "route_geojson": {"type": "LineString", "coordinates": [[-84.51, 39.1], "..."]},
  "map_url": "http://127.0.0.1:8000/api/route/map?start=Cincinnati, OH&finish=Springfield, MO"
}
```

`start` and `finish` take free text (city, ZIP code) or `lat,lng`. Error
cases return JSON with a matching status: `400` for missing parameters, `404`
when a place cannot be resolved, `422` when the route is not drivable within
the range (a gap between truckstops longer than 500 miles), and `502` when
the routing service fails.

Other endpoints: `GET /api/route/map` renders the same trip as an HTML map
page, `GET /api/health` reports station counts, and `GET /` lists the
endpoints.

## How it works

```
GET /api/route?start=...&finish=...
   |
   |-- resolve start and finish     Photon, then Nominatim, cached in the DB
   |-- route                        ONE call to OSRM, cached per coordinate pair
   |-- corridor filter              project truckstops onto the route, keep
   |                                those within 4 miles of it
   |-- fuel plan                    greedy cheapest-fuel strategy
   |-- respond                      JSON (or render the map page)
```

### Corridor filter

Truckstops that are nowhere near the route should not be candidates. OSRM is
asked for the full geometry of the route on purpose: the simplified overview
it offers by default is so coarse that a 500 mile route comes back as a few
dozen points, and stations would be judged against straight lines tens of
miles long. The full line is thinned to half-mile spacing on the server
(one decimation pass, still a single OSRM call), and every station with
known coordinates is projected onto it, vectorized with numpy: a cheap
screening pass against every 50th vertex drops stations nowhere near the
route, then survivors get the exact point-to-segment projection. Stations
within `CORRIDOR_BUFFER_MILES` (4 miles) stay in play, each carrying its
"miles along route" value, which is what the planner works with. The whole
corridor step is a few milliseconds per request.

### Fuel plan

The planner solves the capacity-constrained gas station problem with the
standard greedy:

1. While the destination is out of reach, look at every truckstop within
   reach (strictly ahead on the route).
2. Buy at the cheapest reachable one. On a tie, take the farthest.
3. At that stop, buy just enough fuel to reach the next stop that is equally
   cheap or cheaper, if one fits within a full tank from there. If the trip
   can finish on one tank, buy exactly that amount. Otherwise fill the tank,
   because an expensive stretch lies ahead and it pays to carry the cheap
   fuel as far as possible.

The truck leaves with a full tank that is already paid for, so the first
500 miles are free and the reported cost only counts fuel bought on the way.
Cost per stop is gallons times the retail price from the provided list.

### Speed

One trip needs one OSRM call and one geocoder lookup per uncached endpoint.
Start/finish geocodes are stored in the `GeoCache` table and OSRM responses
in `RouteCache`, so repeating or re-running a trip answers from SQLite. A
fresh cross-country trip takes around 1 to 2 seconds end to end, repeats
answer in 30 to 60 ms.

The 6,738 truckstop addresses were geocoded once during seeding (about an
hour against the free geocoders, politely rate limited) and the result is
committed as `data/stations_geocoded.json`, so neither the API nor a fresh
clone ever needs to geocode the list again. 6,627 of 6,738 resolved; the
remaining 111 had addresses the geocoders could not match, and they are
simply not candidates.

## Project layout

```
spotfleet/            Django project (settings, root urls)
fuel/
  models.py           FuelStation, GeoCache, RouteCache
  views.py            /api/route, /api/route/map, /api/health
  services/
    geo.py            Photon and Nominatim geocoding chain
    osrm.py           OSRM client and route cache
    planner.py        corridor projection (numpy) and fuel planner
    trip.py           orchestration shared by JSON and map views
    http.py           session, user agent, rate limiter
  management/commands/
    load_stations.py  price list to DB, dedupes by OPIS ID
    geocode_stations.py  one-time geocoding seed, writes the snapshot
  templates/fuel/     map page
  tests/              24 tests, external services mocked
data/
  stations_geocoded.json   seeded coordinates snapshot
```

## Tests

```
python manage.py test fuel.tests
```

The suite covers the fuel planner on hand-checked synthetic routes, the
corridor projection math, the geocoding helpers, the price list loader
(duplicate rows), and the API views. External services are mocked, so it
runs offline.

## Assumptions

- Both endpoints are in the USA; the geocoders are restricted to US results.
- The starting tank is full and already paid for, so a trip under 500 miles
  costs nothing in fuel.
- Prices are the retail snapshot from the provided file and never change
  mid-trip.
- Truckstop coordinates come from geocoding the provided addresses. Stops
  that only matched their city centroid are still usable (most truckstop
  towns are small) and are flagged `approx` in the snapshot.
- OSRM's demo server is rate limited and best effort. For anything real you
  would self-host it with Docker behind the same client in `osrm.py`.

## What I would do next

- Model a real truck more closely: diesel prices already are in the data, but
  mpg drops with gross weight, and DOT hours-of-service breaks change where a
  stop makes sense. The planner interface would stay the same, only the
  numbers move.
- Live prices. The provided file is a snapshot; OPIS publishes daily, so the
  FuelStation rows could be refreshed by a scheduled import and the plan
  would follow.
- Sharper station coordinates. The snapshot geocodes the provided addresses
  against free OSM geocoders; a commercial POI dataset (or a self-hosted
  Nominatim) would remove most of the approximate matches.
- If the endpoint list grew, a POST variant with a JSON body plus an OpenAPI
  schema would be the natural next step.
- The corridor filter is a numpy pass over all stations per request (a few
  ms at 6.7k stations). With a much larger list I would put the stations in
  an R-tree or a spatial index first.
