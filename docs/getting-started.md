# Getting started

## Prerequisites

- Home Assistant **2025.1.0 or newer**.
- A Rivian account with a driver profile for your vehicle (the vehicle must be delivered and in your possession).
- Optional: two-factor authentication (2FA) on the Rivian account. It is recommended for security, and **required** if you want remote vehicle controls (see [Vehicle control](vehicle-control.md)).
- Tip: invite a separate driver for Home Assistant (for example `you+ha@example.com`) instead of using your everyday account. That driver must sign in once on the Rivian phone app and be linked to the vehicle before sensors appear.

## Install

### With HACS (recommended)

1. In HACS open the menu (three dots), choose **Custom repositories**, and add this repository's GitHub URL with category **Integration**.
2. Search for **Rivian (Unofficial)** in HACS and download it.
3. Restart Home Assistant.

### Manually

1. Copy the `custom_components/rivian` folder of this repository into the `custom_components` folder of your Home Assistant configuration (create it if needed).
2. Restart Home Assistant.

## First setup

1. Go to **Settings > Devices & services > Add integration** and search for **Rivian (Unofficial)**.
2. Enter your Rivian username and password.
3. If your account uses 2FA, enter the verification code Rivian sends you.
4. Home Assistant creates one device per vehicle with its sensors. Open the integration's **Configure** dialog any time to change the [options](options.md).

## Create the dashboard

The integration ships its own dashboard cards, and the **Rivian** dashboard is created automatically the first time the integration starts (once per Home Assistant instance; if you delete it later it is not recreated). A brand-new dashboard needs one restart of Home Assistant before it shows in the sidebar, and a notification tells you so. If Lovelace is in YAML mode, the dashboard cannot be created automatically; a notification explains this, and you can switch to storage mode and run the action below.

To regenerate or repair the dashboard, open **Developer tools > Actions**, choose **Create Rivian dashboard** (`rivian.create_efficiency_dashboard`) and run it. All fields are optional:

```yaml
action: rivian.create_efficiency_dashboard
data:
  title: Rivian
  icon: mdi:car-electric
  url_path: rivian-dashboard
```

The **Rivian** entry in the sidebar has six tabs: [Vehicles](vehicles.md), [Drives](drives.md), [Fav Routes](fav-routes.md), [Destinations](destinations.md), [Charging](charging.md) and [Efficiency](efficiency.md). Regenerating an existing dashboard needs no restart. Run the action again whenever you add a vehicle or after an upgrade, to refresh the dashboard (see [Upgrading](upgrading.md)). The bundled cards are registered automatically; there is nothing to install separately.

## Try it with demo vehicles

Not driving yet, or want to look around before your own data builds up? Run **Create demo vehicles** (`rivian.create_demo_data`, administrators only). It adds two synthetic vehicles (a Demo R2 and a Demo R1T for a made-up household) with about ten days of drives, places, routes and fast-charge sessions, and regenerates the dashboard. Demo vehicles carry a small **demo** label and never mix with your real data.

Remove them with **Delete demo vehicles** (`rivian.delete_demo_data`), or with "Delete vehicle history..." on a demo vehicle's card on the Vehicles tab.

## Optional: backfill past drives

Drives are recorded live from the moment the integration is set up. If Home Assistant already recorded your vehicle's entities, you can reconstruct earlier drives from its recorder database. **Always do a dry run first**:

```yaml
action: rivian.backfill_drive_history
data:
  days: 30
  dry_run: true
```

The dry run writes nothing; it reports in the Home Assistant log what it would add. When you are happy, run it again with `dry_run: false`. The recorder database is only read, never changed. GPS routes can only be rebuilt for the days the recorder still keeps (about 10 days by default). See [Services](services.md).

## Where your data lives

- Drives, routes, places, charging sessions and battery capacity history are stored in a SQLite file at `config/.storage/rivian_analytics.db` on your Home Assistant host. It is created and upgraded automatically.
- Hourly totals are also written to Home Assistant long-term statistics (`rivian:*`), which Home Assistant never purges.
- Nothing is sent to a Rivian-run analytics service. A few optional lookups go to public services; see [Privacy](privacy.md).

Next: learn the [vehicle bar](vehicle-bar.md) that appears on every tab, then tour the tabs.
