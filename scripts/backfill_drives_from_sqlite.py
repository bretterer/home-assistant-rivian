#!/usr/bin/env python3
"""Standalone CLI tool for Rivian historical drive recorder backfill."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from typing import Any

# Ensure repository root is in sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# If running standalone outside HA, load test mock environment for homeassistant and rivian
if "homeassistant" not in sys.modules or "rivian" not in sys.modules:
    try:
        import rivian  # noqa: F401

        import homeassistant  # noqa: F401
    except ImportError:
        try:
            from tests.conftest import _setup_mock_environment

            _setup_mock_environment()
        except (ImportError, AttributeError) as mock_err:
            logging.getLogger(__name__).debug(
                "Mock environment load skipped: %s", mock_err
            )

from custom_components.rivian.history_backfill import async_backfill_from_recorder


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Backfill Rivian drive history from Home Assistant SQLite recorder database.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Path to Home Assistant SQLite database (e.g. home-assistant_v2.db)",
    )
    parser.add_argument(
        "--vin",
        default=None,
        help="Vehicle VIN to backfill (auto-detected if omitted)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of historical days to backfill (default: all history)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and output statistics without persisting to storage",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Path to save output summary and reconstructed drive records as JSON",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable detailed debug logging and per-drive output",
    )
    return parser.parse_args()


def save_output_json(out_path: str, payload: dict[str, Any]) -> None:
    """Save payload dictionary to JSON file on disk."""
    abs_out = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(abs_out), exist_ok=True)
    with open(abs_out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"\nSaved backfill output to: {abs_out}")


async def main_async() -> int:
    """Async CLI entry point."""
    args = parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not os.path.isfile(args.db_path):
        print(f"Error: Database file not found at '{args.db_path}'", file=sys.stderr)
        return 1

    print("=" * 72)
    print("  Rivian Historical Drive Backfill Engine (Strict Read-Only Mode)")
    print("=" * 72)
    print(f"Database Path : {os.path.abspath(args.db_path)}")
    print(f"Target VIN    : {args.vin or 'Auto-Detect'}")
    print(
        f"Timeframe     : {f'Last {args.days} days' if args.days else 'All available history'}"
    )
    print(
        f"Execution Mode: {'DRY RUN (No changes saved)' if args.dry_run else 'PERSIST'}"
    )
    print("-" * 72)

    try:
        result = await async_backfill_from_recorder(
            hass=None,
            vin=args.vin,
            days=args.days,
            dry_run=args.dry_run,
            db_path=args.db_path,
        )
    except (sqlite3.Error, ValueError, OSError, RuntimeError) as err:
        print(f"\nError during backfill: {err}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 2

    # Display Summary Table
    drives = result.get("drives", [])
    valid_drives_count = result.get("valid_drives", 0)
    micro_drives_count = result.get("micro_drives", 0)
    total_miles = result.get("total_miles", 0.0)
    total_kwh = result.get("total_kwh", 0.0)
    efficiency = result.get("efficiency_mi_kwh", 0.0)
    mpge = result.get("mpge", 0.0)
    duplicates_skipped = result.get("duplicates_skipped", 0)

    print("\nBackfill Results Summary:")
    print(f"  Total Drives Reconstructed : {len(drives)}")
    print(f"  Valid Drives (>= 0.5 mi)   : {valid_drives_count}")
    print(f"  Micro-Drives (< 0.5 mi)    : {micro_drives_count}")
    print(f"  Total Distance Traveled    : {total_miles:.2f} miles")
    print(f"  Total In-Gear Energy Used  : {total_kwh:.2f} kWh")
    print(f"  Weighted Efficiency        : {efficiency:.2f} mi/kWh")
    print(f"  Calculated MPGe            : {mpge:.1f} MPGe")
    print(f"  Duplicate Records Skipped  : {duplicates_skipped}")

    if args.verbose and drives:
        print("\nDrive Details:")
        print(
            f"{'Drive ID':<32} {'Start Time':<20} {'Miles':>7} {'kWh':>7} {'mi/kWh':>7} {'MPGe':>6} {'Elev(ft)':>9}"
        )
        print("-" * 92)
        for d in drives:
            d_dict = d.to_dict() if hasattr(d, "to_dict") else d
            print(
                f"{d_dict.get('drive_id', ''):<32} "
                f"{d_dict.get('start_time', '')[:19]:<20} "
                f"{d_dict.get('distance_miles', 0.0):>7.2f} "
                f"{d_dict.get('energy_kwh', 0.0):>7.2f} "
                f"{d_dict.get('efficiency_mi_kwh', 0.0):>7.2f} "
                f"{d_dict.get('mpge', 0.0):>6.1f} "
                f"{d_dict.get('elevation_change_ft', 0.0):>9.1f}"
            )

    # Save to JSON file if requested
    if args.output:
        serialized_drives = [
            d.to_dict() if hasattr(d, "to_dict") else d for d in drives
        ]
        payload = {
            "summary": {
                "total_drives": len(drives),
                "valid_drives": valid_drives_count,
                "micro_drives": micro_drives_count,
                "total_miles": total_miles,
                "total_kwh": total_kwh,
                "efficiency_mi_kwh": efficiency,
                "mpge": mpge,
                "duplicates_skipped": duplicates_skipped,
            },
            "drives": serialized_drives,
        }
        await asyncio.to_thread(save_output_json, args.output, payload)

    print("\n" + "=" * 72)
    return 0


def main() -> None:
    """CLI script execution."""
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
