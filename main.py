#!/usr/bin/env python3
"""OLCC daily CSV inventory checker with location fallbacks (no maps)."""
import argparse
import csv
import datetime as dt
import json
import logging
import os
import re
import smtplib
import sys
import time
from collections import defaultdict
from email.mime.text import MIMEText
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import requests
from geopy.geocoders import Nominatim

BASE_DIR = Path(__file__).resolve().parent
URL = 'https://www.olcc.state.or.us/pdfs/DailyStoreInventory.csv'
DATA_DIR = BASE_DIR / 'data'
CSV_FILE = DATA_DIR / 'DailyStoreInventory.csv'
CACHE_FILE = DATA_DIR / 'geocode_cache.json'
ITEMS_FILE = BASE_DIR / 'items.txt'
LOG_FILE = BASE_DIR / 'logfile.txt'
PREVIEW_FILE = BASE_DIR / 'last_email.txt'

# Verified against the October 20, 2026 OLCC CSV sample.
COL = {
    'report_date': 1, 'store_number': 2, 'location': 3, 'address': 4,
    'city': 5, 'zip': 6, 'phone': 7, 'hours': 8, 'code': 9,
    'description': 11, 'size': 13, 'quantity': 16, 'price': 17,
    'last_updated': 19,
}


def setup_logging():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(LOG_FILE, encoding='utf-8')])


def load_items(path):
    """One product per line: ITEM_CODE | Friendly product name. # comments allowed."""
    if not path.is_file():
        raise FileNotFoundError(f'Product list missing: {path}')
    items = []
    seen = set()
    for lineno, raw in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('|', 1)
        if len(parts) != 2:
            raise ValueError(f'{path}:{lineno}: expected ITEM_CODE | Product name')
        code, name = (p.strip() for p in parts)
        code = code.upper()
        if not re.fullmatch(r'[A-Z0-9]+', code) or not name:
            raise ValueError(f'{path}:{lineno}: invalid item code or empty name')
        if code in seen:
            raise ValueError(f'{path}:{lineno}: duplicate item code {code}')
        seen.add(code)
        items.append((code, name))
    if not items:
        raise ValueError(f'No products found in {path}')
    logging.info('Loaded %d products from %s', len(items), path)
    return items


def recipients_for_zip(zip_code):
    key = {'97015': 'ADDR_TO_97015', '97223': 'ADDR_TO_97223'}.get(zip_code, 'TEST_EMAIL')
    return [s.strip() for s in os.getenv(key, '').split(',') if s.strip()]


def download_csv():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = CSV_FILE.with_suffix('.csv.part')
    try:
        with requests.get(URL, stream=True, timeout=(20, 180),
                          headers={'User-Agent': 'OLCC-Daily-Inventory-Checker/1.1',
                                   'Accept': 'text/csv,*/*'}) as response:
            logging.info('OLCC download HTTP %s', response.status_code)
            response.raise_for_status()
            with temp.open('wb') as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
        if not temp.exists() or temp.stat().st_size == 0:
            raise ValueError('Downloaded file is empty')
        with temp.open('rb') as file:
            first = file.read(512).lstrip().lower()
        if first.startswith((b'<html', b'<!doctype html')):
            raise ValueError('Received HTML instead of CSV')
        temp.replace(CSV_FILE)
        logging.info('Saved %s (%.2f MiB)', CSV_FILE, CSV_FILE.stat().st_size / 1048576)
    finally:
        if temp.exists():
            temp.unlink()


def read_inventory(items):
    tracked = {code for code, _ in items}
    results = defaultdict(list)
    report_dates = set()
    count = 0
    with CSV_FILE.open(newline='', encoding='utf-8-sig', errors='replace') as file:
        reader = csv.reader(file)
        header = next(reader, None)
        if not header or len(header) <= max(COL.values()) or header[COL['code']].strip() != 'Item Code':
            raise ValueError('Unexpected OLCC CSV header; column positions may have changed')
        for row in reader:
            count += 1
            if len(row) <= max(COL.values()):
                continue
            code = row[COL['code']].strip().upper()
            if code not in tracked:
                continue
            try:
                qty = int(row[COL['quantity']].strip().replace(',', ''))
            except ValueError:
                continue
            if qty <= 0:
                continue
            record = {field: row[index].strip() for field, index in COL.items()}
            record['quantity'] = qty
            results[code].append(record)
            report_dates.add(record['report_date'])
    logging.info('Scanned %s rows; %s positive-stock matches across %s products',
                 count, sum(map(len, results.values())), len(results))
    return results, report_dates


def load_cache():
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError, TypeError):
            logging.warning('Geocoding cache invalid; starting fresh')
    return {}


def save_cache(cache):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = CACHE_FILE.with_suffix('.json.part')
    temp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding='utf-8')
    temp.replace(CACHE_FILE)


def geocode(geolocator, cache, key, query):
    """Only successful lookups are cached; old store:NUMBER entries are supported."""
    if key in cache:
        point = cache[key]
        if isinstance(point, (list, tuple)) and len(point) == 2:
            try:
                return (float(point[0]), float(point[1]))
            except (ValueError, TypeError):
                pass
    for attempt in range(3):
        try:
            location = geolocator.geocode(query, country_codes='us')
            # Nominatim public service: no more than one request per second.
            time.sleep(1.1)
            if location:
                point = [location.latitude, location.longitude]
                cache[key] = point
                save_cache(cache)
                return tuple(point)
            return None
        except Exception as exc:
            logging.warning('Geocode attempt %s failed for %s: %s', attempt + 1, query, exc)
            time.sleep(2)
    return None


def simplified_street(address):
    """Remove trailing suite/unit information that can prevent street matches."""
    result = re.split(r'\s*(?:,\s*|\s+)(?:STE|SUITE|UNIT|APT|APARTMENT|#)\s*[\w-]+.*$',
                      address.strip(), maxsplit=1, flags=re.IGNORECASE)[0]
    return result.strip().rstrip(',')


def miles_between(a, b):
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 3958.7613 * 2 * asin(min(1, sqrt(h)))


def resolve_store(geolocator, cache, number, record):
    address = record['address']
    city = record['city']
    zip_code = record['zip']
    full = f'{address}, {city}, OR {zip_code}, USA'
    point = geocode(geolocator, cache, 'store:' + number, full)
    if point:
        return point, 'street address'

    simple = simplified_street(address)
    if simple and simple.casefold() != address.casefold():
        query = f'{simple}, {city}, OR {zip_code}, USA'
        point = geocode(geolocator, cache, 'store-simple:' + number + ':' + simple.casefold(), query)
        if point:
            logging.info('Store %s resolved using simplified address: %s', number, query)
            return point, 'simplified street address'

    # ZIP center is only a rough location; never label it as a store's precise location.
    if re.fullmatch(r'\d{5}', zip_code):
        point = geocode(geolocator, cache, 'zip:' + zip_code, f'{zip_code}, Oregon, USA')
        if point:
            logging.warning('Store %s using approximate ZIP center (%s): %s', number, zip_code, full)
            return point, 'ZIP center (approximate)'

    logging.warning('Store %s location unverified: %s', number, full)
    return None, 'unverified'


def enrich_with_distances(inventory, zip_code, radius):
    geolocator = Nominatim(user_agent='olcc-daily-inventory-checker/1.1', timeout=20)
    cache = load_cache()
    center = geocode(geolocator, cache, 'zip:' + zip_code, f'{zip_code}, Oregon, USA')
    if center is None:
        raise RuntimeError(f'Unable to geocode search ZIP {zip_code}; refusing to send unfiltered results')

    stores = {}
    for rows in inventory.values():
        for record in rows:
            stores.setdefault(record['store_number'], record)

    locations = {}
    unresolved = set()
    fallback_count = 0
    for number, record in stores.items():
        point, method = resolve_store(geolocator, cache, number, record)
        if point is None:
            unresolved.add(number)
            continue
        if method != 'street address':
            fallback_count += 1
        locations[number] = (point, miles_between(center, point), method)

    filtered = defaultdict(list)
    unverified = defaultdict(list)
    for code, rows in inventory.items():
        for record in rows:
            number = record['store_number']
            info = locations.get(number)
            if info:
                point, distance, method = info
                if distance <= radius:
                    result = dict(record)
                    result.update(coordinates=point, distance_miles=distance, location_method=method)
                    filtered[code].append(result)
            elif number in unresolved:
                # Not counted as within the radius: location is unknown.
                unverified[code].append(dict(record))
        filtered[code].sort(key=lambda row: row['distance_miles'])
        unverified[code].sort(key=lambda row: (row['city'], row['address']))

    logging.info('Located %s/%s stocked stores (%s fallbacks); %s unverified stores; '
                 '%s inventory rows within %s miles',
                 len(locations), len(stores), fallback_count, len(unresolved),
                 sum(map(len, filtered.values())), radius)
    return filtered, unverified


def compose_message(inventory, unverified, items, zip_code, radius, report_dates):
    now = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lines = [now, f'Search ZIP Code: {zip_code}, Search Radius: {radius} miles',
             'Distance is straight-line, not driving distance.',
             'ZIP-center distances are APPROXIMATE and may misclassify stores near the radius boundary.',
             f"OLCC report date(s): {', '.join(sorted(report_dates)) or 'Unknown'}", '']

    any_nearby = False
    for code, name in items:
        records = inventory.get(code, [])
        if not records:
            logging.info('No stock within radius for %s (%s)', name, code)
            continue
        any_nearby = True
        logging.info('%s (%s): %s stores, %s bottles', name, code,
                     len(records), sum(row['quantity'] for row in records))
        lines.append('-------------------------')
        lines.append(f"{name} ({code}): ${records[0]['price']}")
        for row in records:
            approx = ' [ZIP CENTER ESTIMATE]' if row['location_method'].startswith('ZIP center') else ''
            lines.extend([
                f"    - {row['address']}, {row['city']}, OR {row['zip']} (store {row['store_number']})",
                f"      {row['phone']}",
                f"      Quantity: {row['quantity']} | Distance: {row['distance_miles']:.1f} miles{approx}",
            ])
        lines.append('')
    if not any_nearby:
        lines.append('No tracked products with positive stock were located within this radius.')

    if any(unverified.values()):
        lines.extend(['', '=========================',
                      'LOCATION UNVERIFIED — NOT CONFIRMED WITHIN SEARCH RADIUS',
                      'These stores have stock, but their location could not be resolved.',
                      'They may be anywhere in Oregon; check addresses before visiting.', ''])
        for code, name in items:
            records = unverified.get(code, [])
            if not records:
                continue
            lines.append(f'{name} ({code}):')
            for row in records:
                lines.append(f"    - {row['address']}, {row['city']}, OR {row['zip']} "
                             f"(store {row['store_number']}) | Qty: {row['quantity']} | {row['phone']}")
            lines.append('')

    lines.extend(['', 'Inventory is a daily snapshot; call the store to confirm availability.'])
    return '\n'.join(lines)


def send_email(message, recipients, zip_code):
    sender = os.getenv('LOGIN_EMAIL', '').strip()
    password = os.getenv('ACCESS_TOKEN', '').strip()
    if not recipients:
        logging.warning('No recipients configured for ZIP %s; email skipped', zip_code)
        return
    if not sender or not password:
        logging.warning('LOGIN_EMAIL or ACCESS_TOKEN missing; email skipped')
        return
    mail = MIMEText(message, 'plain', 'utf-8')
    mail['From'] = sender
    mail['To'] = ', '.join(recipients)
    mail['Subject'] = 'Blackpink in your area!!! Catch them all for zipcode: ' + zip_code
    with smtplib.SMTP('smtp.gmail.com', 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(sender, password)
        server.sendmail(sender, recipients, mail.as_string())
    logging.info('Email sent to %s recipients', len(recipients))


def main():
    parser = argparse.ArgumentParser(description='OLCC daily CSV inventory checker')
    parser.add_argument('zip', nargs='?', default='97015')
    parser.add_argument('radius', nargs='?', type=int, default=30, choices=[2, 5, 10, 30, 60])
    parser.add_argument('--items', type=Path, default=ITEMS_FILE, help='Product list file (default: items.txt)')
    parser.add_argument('--local-csv', action='store_true', help='Use previously downloaded data/DailyStoreInventory.csv')
    parser.add_argument('--no-email', action='store_true', help='Create results without sending email')
    args = parser.parse_args()
    if not re.fullmatch(r'\d{5}', args.zip):
        parser.error('ZIP code must be five digits')
    setup_logging()
    logging.info('Bot started: ZIP %s, radius %s miles', args.zip, args.radius)
    try:
        items = load_items(args.items)
        if not args.local_csv:
            download_csv()
        elif not CSV_FILE.is_file():
            raise FileNotFoundError(f'--local-csv specified but {CSV_FILE} is missing')
        inventory, dates = read_inventory(items)
        filtered, unverified = enrich_with_distances(inventory, args.zip, args.radius)
        body = compose_message(filtered, unverified, items, args.zip, args.radius, dates)
        PREVIEW_FILE.write_text(body, encoding='utf-8')
        logging.info('Email preview saved to %s', PREVIEW_FILE)
        if not args.no_email:
            send_email(body, recipients_for_zip(args.zip), args.zip)
        logging.info('Bot completed successfully')
    except Exception:
        logging.exception('Bot failed')
        sys.exit(1)


if __name__ == '__main__':
    main()
