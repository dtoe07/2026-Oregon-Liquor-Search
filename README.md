# Oregon Liquor Inventory Search

This project checks the Oregon Liquor and Cannabis Commission (OLCC) daily
store-inventory CSV for selected products, finds stores near a ZIP code, and
creates an email-ready inventory report. It can be run locally or on a
schedule with GitHub Actions.

Distances are straight-line estimates, not driving distances. Inventory is a
daily snapshot, so call the store to confirm availability before visiting.

## How it works

1. Reads product codes and display names from [`items.txt`](./items.txt).
2. Downloads the current OLCC `DailyStoreInventory.csv` unless local data was
   requested.
3. Keeps only tracked products with positive stock.
4. Geocodes the search ZIP and matching store addresses with Nominatim.
5. Caches successful geocoding results in `data/geocode_cache.json`.
6. Filters stores to the requested radius and writes the report to
   `last_email.txt`.
7. Sends the report by email unless `--no-email` is specified.

If a store address cannot be geocoded, it is shown separately as
**location unverified** rather than being treated as within the search radius.

## Requirements

- Python 3.11 or newer
- Network access to the OLCC inventory URL and the Nominatim geocoding service
- Gmail SMTP credentials if email delivery is enabled

Install the Python dependencies in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Usage

The positional arguments are a five-digit ZIP code and a supported radius.
Defaults are ZIP `97015` and a 30-mile radius.

```bash
# Download current OLCC data, create a report, and send email
python main.py 97015 30

# Generate a report without sending email
python main.py 97223 10 --no-email

# Use data already saved at data/DailyStoreInventory.csv
python main.py 97015 30 --local-csv --no-email

# Use a different product list
python main.py 97015 30 --items /path/to/items.txt --no-email
```

Supported radii are `2`, `5`, `10`, `30`, and `60` miles. The command writes:

- `last_email.txt` — the latest report
- `logfile.txt` — execution logs
- `data/DailyStoreInventory.csv` — the downloaded OLCC data
- `data/geocode_cache.json` — successful geocoding results

These generated files are ignored by Git.

## Product list

Add one product per line to [`items.txt`](./items.txt) using this format:

```text
ITEM_CODE | Product name
```

Blank lines and lines beginning with `#` are ignored. Item codes must contain
only letters and numbers, and duplicate codes are rejected.

## Email configuration

Email is sent through Gmail SMTP on port 587. Set these environment variables
before running the command:

| Variable | Purpose |
| --- | --- |
| `LOGIN_EMAIL` | Gmail account used to send the report |
| `ACCESS_TOKEN` | Gmail app password or SMTP credential |
| `ADDR_TO_97015` | Comma-separated recipients for ZIP `97015` |
| `ADDR_TO_97223` | Comma-separated recipients for ZIP `97223` |
| `TEST_EMAIL` | Fallback recipients for other ZIP codes |

If recipients or SMTP credentials are missing, the report is still generated
but email delivery is skipped with a warning. Keep credentials in environment
variables or GitHub repository secrets; do not commit them.

## GitHub Actions

The [`inventory-bot.yml`](./.github/workflows/inventory-bot.yml) workflow runs
daily at 14:00 UTC and can also be started manually. It currently checks:

- ZIP `97015` with a 30-mile radius
- ZIP `97223` with a 30-mile radius

Configure these repository secrets for automated email delivery:

`LOGIN_EMAIL`, `ACCESS_TOKEN`, `ADDR_TO_97015`, `ADDR_TO_97223`, and
`TEST_EMAIL`.

Each matrix job restores and saves its geocoding cache. After each run, the
workflow uploads the cache and the generated `logfile.txt` and
`last_email.txt` as artifacts for up to seven days.

The scheduled time is expressed in UTC. The workflow runs at minute 7 to avoid
the top-of-the-hour congestion that can delay GitHub Actions scheduled runs.
Update the workflow cron expression if the desired local delivery time changes
with daylight saving time.

## Data and geocoding notes

- The OLCC CSV column layout is validated when it is read; a changed header
  causes the run to fail rather than silently producing incorrect results.
- Nominatim requests are rate-limited to approximately one request per second.
  A first run can therefore take several minutes.
- Store addresses are tried first, followed by a simplified street address and
  then an approximate ZIP-center fallback.
- ZIP-center results are labeled as approximate in the report and may be
  inaccurate near the radius boundary.
- Prices and quantities come from the downloaded OLCC report and may change
  after the report is generated.

## Project files

| File | Description |
| --- | --- |
| [`main.py`](./main.py) | Command-line inventory search and report generator |
| [`items.txt`](./items.txt) | Tracked OLCC item codes and display names |
| [`requirements.txt`](./requirements.txt) | Python dependencies |
| [`data/`](./data/) | Downloaded inventory and geocoding cache |
| [`.github/workflows/inventory-bot.yml`](./.github/workflows/inventory-bot.yml) | Scheduled GitHub Actions workflow |
