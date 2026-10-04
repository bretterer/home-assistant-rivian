# FAQ and troubleshooting

## The dashboard looks broken or shows old cards after upgrading

Browsers cache the bundled cards. Reload the page with a hard refresh (Ctrl+Shift+R, or Cmd+Shift+R on macOS); in the Home Assistant mobile app use **Settings > Companion app > Debugging > Reset frontend cache**, then reopen it. If a card still shows an old layout, run `rivian.create_efficiency_dashboard` again (see below).

## How do I regenerate the dashboard?

Run **Create Rivian dashboard** from Developer tools > Actions (see [Services](services.md)). It rewrites the dashboard in place; use a different `url_path` if you want a second copy. Do this after adding a vehicle, deleting demo data, or upgrading.

## Charging history is missing sessions from before the upgrade

Home (AC) charging is recorded from the moment you are on 2.0; earlier home charges were not captured live. Run `rivian.import_charging_history` to pull completed sessions from Rivian's app history.

## Importing charging history returns nothing

The Rivian history only includes sessions the Rivian account can see. If your Home Assistant account is a separate invited driver, or Rivian returned no sessions, the history is empty. Fast charges you did while Home Assistant was running are still recorded live. Demo vehicles never import.

## Old drives have no GPS route

Routes are recorded live from the time route capture is on (**Store GPS routes** in [Options](options.md)). Drives recorded before that, or before upgrading, only have summary data. A backfill can rebuild routes for about the last 10 days (the recorder's location history), see [Getting started](getting-started.md#optional-backfill-past-drives). Without a route, a drive does not appear on maps or the heat map, and cannot create a favorite-route variant.

## A drive starts a minute after the car moved

The car can take a minute or two after waking to report its position. The day still starts where the car was parked and the unrecorded stretch is drawn dashed. If a stream drops mid-drive, the straight line across the gap is replaced by a road-snapped path when the roads can be matched.

## Destinations show "Place #3" instead of a name

Unnamed suggestions get a name from OpenStreetMap when naming is enabled; if it is off or found nothing, name it yourself (administrators) on the [Destinations tab](destinations.md).

## Favorite routes are missing

A route needs at least three drives between the same two named places. Check that both ends have places, and press `rivian.rebuild_routes` if you changed many places.

## Efficiency scores or the Model view are empty

They need about 15 drives with a GPS route in the last 90 days, and weather for those drives. Run `rivian.backfill_weather` then `rivian.fit_energy_model`.

## Controls do not work

See [Vehicle control](vehicle-control.md): 2FA, Bluetooth pairing and Gen1 (pre-2025) vehicles are required, and the vehicle must be in Park.

## Remove the demo vehicles

Run `rivian.delete_demo_data`, or use "Delete vehicle history..." on a demo card.

## Where is my data, and how big is it?

In `.storage/rivian_analytics.db` on the Home Assistant host. The Drives tab footer shows how many routes are stored and their size. Reduce it with the retention options in [Options](options.md).

## Something is wrong; how do I report it?

Open an issue with the integration's debug log (add `custom_components.rivian: debug` under `logger:` in `configuration.yaml`) and the browser console output if it is a display problem. Remove VINs and locations from anything you post.
