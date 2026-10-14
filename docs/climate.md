# Climate — ESP8266 / BME280

The existing camera server now also stores environmental measurements. The
**Climate** tab at `/climate/` shows temperature, humidity, pressure and battery
voltage, with a selectable history, hourly temperature map and CSV export.
The shared Telemetry style applies to live cameras, recordings, login and
the monitoring pages, in light and dark themes. The minimalist layout uses
system typography, neutral backgrounds, blue accents and open sections
instead of individually bordered measurement cards.
New visits default to the light theme with a white background, regardless of
the operating system's appearance. An explicitly selected theme is remembered;
the administration panel also defaults to light on its first visit.

The Telemetry wordmark uses a monochrome signal symbol shared by the navigation,
sign-in and administration pages. Tab icons are provided as SVG and a six-size
ICO, plus a 180 px Apple touch icon. Their links are included on authentication
and error pages as well; `/favicon.ico` redirects to the static ICO without
requiring a login. To rebuild the raster icons after editing `favicon.svg`, run
`python scripts/generate_brand_icons.py` with Pillow installed. Increment the
version in `stream/includes/favicons.html` and the root redirect when replacing
icons so browsers can refresh their cached copies.

## Connect a station

1. Apply the database migration using the server's existing environment:

   ```sh
   cd server
   python manage.py migrate
   python manage.py collectstatic --noinput
   ```

   Docker startup already performs both steps. Rebuild/restart using the
   existing server management script when deploying updated source.
   `./manage_server.sh start network` now rebuilds from local source using
   Docker's build cache. Network mode persists its database in the
   `database_data` volume, so container updates retain accounts and readings.
   When upgrading an older network-mode installation without a database
   mount, back up its `/app/data/db.sqlite3` and transfer it to this volume
   before recreating the server container.

2. Create a sensor in Django Admin → **Sensors**. Set its name, a unique
   slug (`garden`), lowercase MAC address (`aa:bb:cc:dd:ee:ff`), location and
   expected interval (300 seconds for the current firmware). Assign accounts
   under **Allowed users**. The token is generated on first save; reopen the
   sensor to copy it. The “Generate new auth token” action rotates it.

   Alternatively, register it from the command line:

   ```sh
   python manage.py register_sensor \
     --mac aa:bb:cc:dd:ee:ff --sensor-id garden \
     --name 'Garden' --location 'North side' --user YOUR_USERNAME
   ```

   The command prints the new token once. Repeating the command preserves
   the token and adds the specified readers. Use Admin to edit existing
   metadata, or `--rotate-token` to replace a token. A reader must be explicitly
   assigned even when their account is a superuser, matching camera permissions.

3. In `weather_station_ESP8266.nosync/firmware/src/main.py`, configure:

   ```python
   API_url = 'https://YOUR-SERVER/api/measurements'
   AUTH_TOKEN = 'TOKEN_FOR_THIS_SENSOR'
   SSID = 'YOUR_WIFI'
   PASSWD = 'YOUR_WIFI_PASSWORD'
   ```

   Use the base URL without a trailing slash: existing firmware appends
   `/timesync` and `/timedelta/<seconds>`. Each station uses its own token;
   tokens from the archived IoT_API are not imported. Use the device's actual
   MAC from `network.WLAN().config('mac')`.

4. Copy the firmware files, including the updated `APIrequests.py`, to the
   ESP8266 and restart it. Open **Climate** while logged in to an assigned
   account. Actual I2C operation, Wi-Fi, HTTPS support and deep sleep must be
   verified on the board; the automated tests use simulated hardware.

## Production upgrade

This update includes migration `0006_environmental_sensors`, not just new
templates. Before updating production, take a consistent backup of its SQLite
database (use SQLite's backup API or `.backup` for a running database). Keep the
existing `.env`, database and recordings on the server; they do not belong in
the public repository. Production already mounts `server/data` persistently.

After pulling the committed source, run from the repository root:

```sh
./manage_server.sh rebuild prod
docker compose -f server/docker-compose.prod.yml exec -T server python manage.py showmigrations stream
docker compose -f server/docker-compose.prod.yml exec -T proxy-bridge nginx -t
docker compose -f server/docker-compose.prod.yml exec -T proxy-bridge nginx -s reload
```

Startup applies migrations and collects static assets. Confirm that migration
`0006_environmental_sensors` is marked `[X]`. Reloading the proxy activates the
updated sensor endpoint rate limit in the mounted Nginx configuration. Finally,
check sign-in, video playback and an authenticated Climate page on the actual
production domain. Provision sensors and assign their readers separately;
committing source does not transfer local accounts, device tokens or readings.

## Device API

`POST /api/measurements` (also accepts `/api/measurements/` without redirect)
requires `Content-Type: application/json` and
`Authorization: Token <device-token>` (Bearer is also accepted).

```json
{
  "mac_address": "aa:bb:cc:dd:ee:ff",
  "temperature": 21.35,
  "humidity": 55.2,
  "pressure": 1013.25,
  "adc": 3.92,
  "timestamp": "2026-10-13T10:00:00Z"
}
```

`temperature` and `mac_address` are required. Humidity, pressure and `adc`
may be omitted or null; temperature-only devices work too. Units are °C,
%, hPa and volts. `adc` is already converted to voltage by this firmware,
and is stored as `battery_voltage`. This is a voltage reading, not a battery
percentage. Numeric strings, booleans, NaN, infinity and out-of-range values
are rejected. Ranges: temperature −80…125 °C, humidity 0…100%, pressure
300…1200 hPa, battery 0…6 V. Maximum payload size is 8 KiB.

Without `timestamp`, the server timestamps receipt. With an explicit
timestamp, a repeated identical measurement returns `200` with
`created: false`; a different measurement at the same timestamp returns
`409` without replacing history. New measurements return `201`.
Idempotency therefore requires an explicit, stable timestamp on retries.
Unknown/inactive devices or invalid tokens return `401`; invalid data returns
`400`. Devices never register themselves and cannot read private history.
The production proxy gives `/api/measurements` a separate limit of 10 requests
per second per source IP with a burst of 50, allowing backup recovery without
using the browser API's 30-requests-per-minute allowance. Excess requests
return `429`; updated firmware retains those measurements for its next wake.

`GET /api/measurements/timesync` returns the MicroPython RTC tuple in UTC:

```json
{"datetime_now": [2026, 10, 13, 1, 10, 0, 0, 0], "timezone": "UTC"}
```

`GET /api/measurements/timedelta/300` returns an ISO 8601 timestamp five minutes
in the past, for the existing firmware's backup recovery flow. Offsets are
limited to 366 days. These two clock endpoints are public and never return
measurement data. Naive legacy timestamps are interpreted as UTC; browser
times use the server's configured `TIME_ZONE`, default `Europe/Warsaw`.

The legacy firmware estimates backup times from the sleep interval and file
order. Those reconstructed times are approximate; this integration preserves
that existing behavior. A timestamp before 2000 or more than five minutes in
the future is rejected. Delayed measurements do not replace the newest reading.

## Browser API and display

- `GET /api/climate/?sensor=garden&range=24h`: assigned stations, latest
  reading, complete-period statistics, chart series, last 20 readings and
  hourly temperatures from the last day.
- `GET /api/climate/export/?sensor=garden&range=24h`: streaming CSV of all
  raw readings in that period, oldest first. The UTF-8 BOM supports Excel.
- Supported ranges: `1h`, `24h`, `7d`, `10d`, `30d`.

Both endpoints require an authenticated browser session and explicit sensor
access. Device tokens never grant dashboard access. Responses are not cached.
The dashboard polls every 30 seconds while visible and refreshes on returning
to the tab; it reports request failures while preserving the last visible data.

The device is online when it has contacted the server within three configured
sampling intervals; missing values and gaps remain visible as missing data.
Charts use minute averages for 1 h / 24 h and hourly averages for longer
periods, bounding the response size. Summary statistics use every raw reading
in the selected period. The CSV retains full measurement precision.
Charts support pointer inspection and keyboard navigation using the slider
below the plot. Recent raw readings are also available as a table.

## Tests

```sh
cd server
DJANGO_SECRET_KEY=test-key DJANGO_DEBUG=1 DJANGO_ALLOWED_HOSTS='*' \
  PYTHONPATH=.. venv/bin/python manage.py test \
  tests.test_climate tests.test_logging_security tests.test_2fa --noinput
```

The separate firmware project includes HTTP-error and backup-retention tests:

```sh
cd ../weather_station_ESP8266.nosync
python3 -m unittest discover -s local_dev -p 'test_*.py'
python3 local_dev/run.py --offline --state-dir /tmp/weather-station-check
python3 local_dev/run.py --state-dir /tmp/weather-station-check
```
