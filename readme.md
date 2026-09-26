# WarsawGTFS (personal fork)

- This is a fork of [MKuranowski/WarsawGTFS](https://github.com/MKuranowski/WarsawGTFS). See that repository for the project's documentation.
- `personal` (the default branch) is upstream plus my own changes, which will never be sent upstream. Upstream is merged into it automatically every day.
- `master` is an exact copy of upstream's `master`. It is fast-forwarded daily and never committed to directly.
- A GTFS feed is built from this branch every night and published at <https://436.pl/gtfs/warsaw.zip> (`.github/workflows/build-gtfs.yml`). It should match <https://mkuran.pl/gtfs/warsaw.zip> apart from the feed publisher, the build time and some OSM-based bus shapes; `.github/workflows/compare-gtfs.yml` checks that every morning using `tools/compare_gtfs.py`.
