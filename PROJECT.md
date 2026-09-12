# Project: Rivian Trip Efficiency & Analytics Subsystem

## Architecture
The subsystem provides native trip efficiency (`mi/kWh`), MPGe tracking, real-time drive lifecycle management, historical recorder backfilling, and Lovelace dashboard cards for Home Assistant's Rivian integration.

```
+-----------------------------------------------------------------------------------+
|                            Home Assistant Runtime                                 |
|                                                                                   |
|  +---------------------------+          +--------------------------------------+  |
|  |     VehicleCoordinator    |          |    home-assistant_v2.db (SQLite)     |  |
|  | (GraphQL WebSocket feeds) |          | (Read-Only mode=ro connection URI)   |  |
|  +-------------+-------------+          +-------------------+------------------+  |
|                | update listener                            | read-only queries   |
|                v                                            v                     |
|  +---------------------------+          +--------------------------------------+  |
|  |     DriveTracker Engine   |          |      History Backfill Engine         |  |
|  |  - Park -> Drive trigger  |          |  - Reconstruct past drive trips      |  |
|  |  - 60s Park debounce      |          |  - Open-Meteo Historical Archive API |  |
|  |  - GPS lock gate (>2mph)  |          |  - Deduplication & aggregation       |  |
|  |  - 10 mph speed binning   |          +-------------------+------------------+  |
|  |  - Open-Meteo Live API    |                              |                     |
|  |  - Micro-drive filter     |                              |                     |
|  +-------------+-------------+                              |                     |
|                |                                            |                     |
|                +---------------------+----------------------+                     |
|                                      |                                            |
|                                      v                                            |
|                    +-----------------------------------+                          |
|                    |     DriveStore / Storage Helper   |                          |
|                    | (<config>/.storage/               |                          |
|                    |    rivian_drives_{vin}.json)      |                          |
|                    +-----------------+-----------------+                          |
|                                      |                                            |
|                                      v                                            |
|                    +-----------------------------------+                          |
|                    |      Entity Platform (sensor.py)  |                          |
|                    |  - 8 Primary & Diagnostic Sensors |                          |
|                    |  - 30-Day & All-Time Aggregations |                          |
|                    |  - Strings & Translations         |                          |
|                    +-----------------+-----------------+                          |
|                                      |                                            |
|                                      v                                            |
|                    +-----------------------------------+                          |
|                    |     Lovelace Dashboard Package    |                          |
|                    | (lovelace_efficiency_dashboard)   |                          |
|                    +-----------------------------------+                          |
+-----------------------------------------------------------------------------------+
```

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F1 | Storage & Data Schema | Isolated JSON storage via `Store` in `.storage/rivian_drives_{vin}.json`, schema v1.1, CRUD, deduplication, clean reset with 0 SQLite writes | M1 | R2 |
| F2 | Drive Lifecycle Engine | Real-time drive detection on gear shift, 60s debounce, GPS lock sync validation (>2 mph / >50m) | M2 | R1 |
| F3 | Energy & Speed Binning | Delta SOC energy consumption (kWh), elevation delta (ft), 10 mph speed bin accumulators (0-9 to 80+ mph) | M2 | R1 |
| F4 | Route Weather Integration | Open-Meteo REST API live weather sampling (every 15 mi / 20 min) and distance-weighted integrated temperature | M2 | R1 |
| F5 | Micro-Drive Filtering | 0.5-mile threshold tagging and exclusion from rolling 30-day & all-time efficiency calculations | M2 | R1 |
| F6 | Entity Platform (8 Sensors) | Expose `last_drive_efficiency`, `efficiency_30d`, `efficiency_all_time`, `last_drive_distance`, `last_drive_mpge`, `mpge_30d`, `mpge_all_time`, `drive_status` with measurement state classes and precisions | M3 | R3 |
| F7 | Translations & Localization | Add entity names and descriptions to `strings.json` and `translations/en.json` | M3 | R3 |
| F8 | Historical Backfill Service | `async_backfill_from_recorder` service with read-only SQLite (`mode=ro`), drive trip reconstruction, Open-Meteo Archive API integration, and deduplication | M4 | R4 |
| F9 | Standalone Backfill CLI | `scripts/backfill_drives_from_sqlite.py` with `--dry-run` and `--output` flags | M4 | R4 |
| F10 | Lovelace Dashboard Package | `lovelace_efficiency_dashboard.yaml` featuring Mushroom/Tile overview cards, Plotly elevation-colored scatterplot, and speed bin bar chart | M5 | R5 |
| F11 | Empirical Baseline & E2E Validation | Pass 100% of test suite and validate 10-day dataset for R1S test vehicle (370.93 mi, 131.05 kWh, 2.83 mi/kWh, 95.4 MPGe across 58 drives) and ruff linting | M6 | Acceptance Criteria |

## Code Layout
- `custom_components/rivian/drive_models.py` — Data models (`DriveRecord`, `SpeedBinData`, `AggregatedDriveStats`, `DriveState`, `DriveStatus`) and serialization helpers.
- `custom_components/rivian/drive_storage.py` — Storage manager using `hass.helpers.storage.Store`.
- `custom_components/rivian/weather.py` — Open-Meteo live forecast and historical archive client with caching and fallback.
- `custom_components/rivian/drive_tracker.py` — Real-time drive lifecycle engine, state machine, debouncer, speed bin accumulator, and weather sampler.
- `custom_components/rivian/history_backfill.py` — Historical recorder backfill engine querying SQLite in read-only mode (`mode=ro`).
- `custom_components/rivian/sensor.py` — Entity platform registering the 8 drive efficiency and status sensors.
- `custom_components/rivian/const.py` — Subsystem constants, domain keys, conversion factors (33.705 MPGe, etc.).
- `custom_components/rivian/strings.json` & `translations/en.json` — Entity and option translation definitions.
- `custom_components/rivian/__init__.py` — Integration entry setup, drive tracker initialization, service registration, cleanup on unload.
- `scripts/backfill_drives_from_sqlite.py` — Standalone CLI script for database migration and dry-run verification.
- `lovelace_efficiency_dashboard.yaml` — Turnkey Lovelace cards (Mushroom, Tile, Plotly scatterplot, Speed bin chart).
- `tests/` — Test infrastructure, fixtures, unit tests, and empirical baseline verification suite (122 tests).

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Isolated Storage & Data Schema | `drive_models.py`, `drive_storage.py` (F1) | none | DONE |
| M2 | Real-Time Drive Tracker & Weather | `weather.py`, `drive_tracker.py` (F2, F3, F4, F5) | M1 | DONE |
| M3 | Entity Platform & Translations | `const.py`, `sensor.py`, `strings.json`, `en.json` (F6, F7) | M1, M2 | DONE |
| M4 | Historical Backfill Engine & CLI | `history_backfill.py`, `scripts/backfill_drives_from_sqlite.py`, service registration in `__init__.py` (F8, F9) | M1, M2 | DONE |
| M5 | Turnkey Lovelace Dashboard | `lovelace_efficiency_dashboard.yaml` (F10) | M3 | DONE |
| M6 | Test Infra, E2E Baseline & Lint Gate | `tests/`, test runner, empirical baseline fixture, ruff clean verification (F11) | M1-M5 | DONE |

## Interface Contracts

### `DriveRecord` Data Model
```python
@dataclass
class DriveRecord:
    vin: str
    drive_id: str  # f"{vin}_{start_epoch_s}"
    start_time: str  # ISO-8601 UTC
    end_time: str  # ISO-8601 UTC
    distance_miles: float
    duration_seconds: float
    start_soc: float  # Percentage
    end_soc: float  # Percentage
    battery_capacity_kwh: float  # Pack capacity in kWh
    energy_kwh: float  # Gross energy consumed in-gear
    efficiency_mi_kwh: float  # distance / energy (if energy > 0 else 0.0)
    mpge: float  # efficiency_mi_kwh * 33.705
    start_altitude_ft: float
    end_altitude_ft: float
    elevation_change_ft: float  # end_altitude_ft - start_altitude_ft
    avg_speed_mph: float
    max_speed_mph: float
    integrated_temperature_f: float | None
    speed_bins: dict[str, float]  # {"0-9": miles, "10-19": miles, ..., "80+": miles}
    is_micro_drive: bool  # distance_miles < 0.5
```

### `DriveStore` Interface
```python
class DriveStore:
    def __init__(self, hass: HomeAssistant, vin: str) -> None: ...
    async def async_load(self) -> list[DriveRecord]: ...
    async def async_save_drive(self, drive: DriveRecord) -> bool: ... # Deduplicates by drive_id
    async def async_get_drives(self, min_distance: float = 0.0) -> list[DriveRecord]: ...
    def get_stats_30d(self) -> AggregatedDriveStats: ...
    def get_stats_all_time(self) -> AggregatedDriveStats: ...
    async def async_reset(self) -> None: ...
```

### `DriveTracker` Interface
```python
class DriveTracker:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: VehicleCoordinator, vehicle_info: dict[str, Any], store: DriveStore) -> None: ...
    async def async_setup(self) -> None: ...
    def handle_coordinator_update(self) -> None: ...
    async def async_unload(self) -> None: ...
```

### `HistoryBackfill` Service & CLI Interface
```python
async def async_backfill_from_recorder(
    hass: HomeAssistant,
    vehicle_id: str,
    vin: str,
    days: int | None = None,
    dry_run: bool = False,
    db_path: str | None = None,
) -> dict[str, Any]:
    # Returns {"drives_found": int, "total_miles": float, "total_kwh": float, "efficiency_mi_kwh": float, "mpge": float, "duplicates_skipped": int}
```
