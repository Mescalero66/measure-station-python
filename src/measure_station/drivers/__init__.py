"""Hardware drivers, copied verbatim from drivers-repo.

Don't edit them here: change them in drivers-repo and copy them over, so both stay
identical. That's also why ruff excludes this folder.

Each driver is used by a station adapter in ``measure_station.sensors``, which imports
it inside ``open()``. Drivers import their hardware libraries (e.g. ``smbus2``) at the
top, so importing one at module level would break the station on machines without them.
"""
