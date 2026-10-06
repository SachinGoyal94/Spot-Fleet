from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from fuel.services import osrm
from fuel.services.geo import GeocodeError
from fuel.services.planner import InfeasibleRoute
from fuel.services.trip import StationRepo, build_trip


def _error(message, status):
    return JsonResponse({'error': message}, status=status)


@require_GET
def route_api(request):
    """GET /api/route?start=<place|lat,lng>&finish=<place|lat,lng>

    Returns the route (GeoJSON), the optimal fuel stops with per-stop cost,
    and the total money spent on fuel.
    """
    start_q = request.GET.get('start', '')
    finish_q = request.GET.get('finish', '')
    if not start_q or not finish_q:
        return _error("Both 'start' and 'finish' query parameters are required.", 400)

    try:
        trip = build_trip(start_q, finish_q)
    except GeocodeError as exc:
        return _error(str(exc), 404)
    except osrm.RoutingError as exc:
        return _error(str(exc), 502)
    except InfeasibleRoute as exc:
        return _error(str(exc), 422)

    trip['map_url'] = request.build_absolute_uri(
        f"{reverse('route-map')}?start={start_q}&finish={finish_q}"
    )
    return JsonResponse(trip)


@require_GET
def route_map(request):
    """GET /api/route/map?start=...&finish=...  -> interactive Leaflet map."""
    start_q = request.GET.get('start', '')
    finish_q = request.GET.get('finish', '')
    if not start_q or not finish_q:
        return _error("Both 'start' and 'finish' query parameters are required.", 400)

    try:
        trip = build_trip(start_q, finish_q)
    except GeocodeError as exc:
        return render(request, 'fuel/map_error.html', {'message': str(exc)}, status=404)
    except (osrm.RoutingError, InfeasibleRoute) as exc:
        return render(request, 'fuel/map_error.html', {'message': str(exc)}, status=502)

    context = {
        'start_label': trip['start']['label'],
        'finish_label': trip['finish']['label'],
        'total_distance_miles': trip['total_distance_miles'],
        'total_fuel_cost_usd': trip['total_fuel_cost_usd'],
        'total_gallons': trip['total_gallons_purchased'],
        'estimated_drive_hours': trip['estimated_drive_hours'],
        'stops': trip['fuel_stops'],
        'geojson': trip['route_geojson'],
    }
    return render(request, 'fuel/map.html', context)


@require_GET
def health(request):
    stations = StationRepo.all_geocoded()
    return JsonResponse({
        'status': 'ok',
        'geocoded_stations_available': len(stations),
    })


def api_index(request):
    return JsonResponse({
        'name': 'Spot Fleet fuel-optimized routing API',
        'endpoints': {
            'route': '/api/route?start=<place>&finish=<place>',
            'map': '/api/route/map?start=<place>&finish=<place>',
            'health': '/api/health',
        },
        'example': '/api/route?start=Cincinnati, OH&finish=Springfield, MO',
        'notes': 'start/finish accept free text (US) or "lat,lng".',
    })
