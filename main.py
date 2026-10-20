#!/usr/bin/env python3
"""OLCC daily CSV inventory checker and email notifier (maps disabled)."""
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
from pathlib import Path
from math import radians, sin, cos, asin, sqrt

import requests
from geopy.geocoders import Nominatim

URL = 'https://www.olcc.state.or.us/pdfs/DailyStoreInventory.csv'
DATA_DIR = Path('data')
CSV_FILE = DATA_DIR / 'DailyStoreInventory.csv'
CACHE_FILE = DATA_DIR / 'geocode_cache.json'
LOG_FILE = Path('logfile.txt')

ITEM_LIST = [
    ('8722B', 'Red Weller'),
    ('8954B', 'Green Weller'),
    ('1562B', 'W.L. WELLER 12YR KENTUCKY STRAIGHT BRBN'),
    ('6374B', 'WELLER FULL PROOF'),
    ('11941B', 'WELLER FULL PROOF SINGLE BARREL SELECT'),
    ('12941I', 'OLD FITZGERALD 7 YEAR BIB'),
    ('8547B', 'ELMER T. LEE STR. BOURBON'),
    ('0191B', 'Stagg JR.'),
    ('2146B', 'WOODFORD RES. MC BOURBON'),
    ('7256B', "MICHTER'S S/B 10 YR"),
    ('2893B', 'HIGH WEST MIDWINTER'),
    ('6550B', "MICHTER'S TOASTED BARREL FINISH"),
    ('0793B', 'E.H. TAYLOR SINGLE BARREL STRAIGHT BOURB'),
    ('1416B', 'E.H. TAYLOR JR BARREL PROOF'),
    ('1418B', 'E.H. TAYLOR STRAIGHT RYE WHISKEY'),
    ('5830B', 'TEQUILA FORTALEZA BLANCO'),
    ('5831B', 'TEQUILA FORTALEZA REPOSADO'),
    ('5829B', 'TEQUILA FORTALEZA Anejo'),
    ('2160B', 'EAGLE RARE S/B BOURBON'),
    ('1059B', 'HAKUSHU 12 YR'),
    ('3560B', 'HAKUSHU 18 YR'),
]

# Verified against the October 20, 2026 OLCC CSV sample.
COL = {
    'report_date': 1, 'store_number': 2, 'location': 3, 'address': 4,
    'city': 5, 'zip': 6, 'phone': 7, 'hours': 8, 'code': 9,
    'description': 11, 'size': 13, 'quantity': 16, 'price': 17,
    'last_updated': 19,
}


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding='utf-8')],
    )


def recipients_for_zip(zip_code):
    key = {'97015': 'ADDR_TO_97015', '97223': 'ADDR_TO_97223'}.get(zip_code, 'TEST_EMAIL')
    return [s.strip() for s in os.getenv(key, '').split(',') if s.strip()]


def download_csv():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = CSV_FILE.with_suffix('.csv.part')
    try:
        with requests.get(URL, stream=True, timeout=(20, 180),
                          headers={'User-Agent': 'OLCC-Daily-Inventory-Checker/1.0',
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


def read_inventory():
    tracked = {code for code, _ in ITEM_LIST}
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
        except (OSError, ValueError):
            logging.warning('Geocoding cache invalid; starting fresh')
    return {}


def save_cache(cache):
    DATA_DIR.mkdir(exist_ok=True)
    temp = CACHE_FILE.with_suffix('.json.part')
    temp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding='utf-8')
    temp.replace(CACHE_FILE)


def geocode(geolocator, cache, key, query):
    if key in cache:
        point = cache[key]
        return tuple(point) if point else None
    for attempt in range(3):
        try:
            location = geolocator.geocode(query, country_codes='us')
            # Nominatim's public service requires at most 1 request/second.
            time.sleep(1.1)
            if location:
                point = [location.latitude, location.longitude]
                cache[key] = point
                save_cache(cache)
                return tuple(point)
            break
        except Exception as exc:
            logging.warning('Geocode attempt %s failed for %s: %s', attempt + 1, query, exc)
            time.sleep(2)
    # Don't cache misses permanently: a later run may succeed.
    return None


def miles_between(a, b):
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 3958.7613 * 2 * asin(min(1, sqrt(h)))


def enrich_with_distances(inventory, zip_code, radius):
    geolocator = Nominatim(user_agent='olcc-daily-inventory-checker/1.0', timeout=20)
    cache = load_cache()
    center = geocode(geolocator, cache, 'zip:' + zip_code, f'{zip_code}, Oregon, USA')
    if center is None:
        raise RuntimeError(f'Unable to geocode ZIP {zip_code}; refusing to send unfiltered results')
    # Deduplicate geocoding across products. Only positive-stock stores are queried.
    stores = {}
    for rows in inventory.values():
        for record in rows:
            store = record['store_number']
            if store not in stores:
                stores[store] = record
    locations = {}
    for number, record in stores.items():
        address = f"{record['address']}, {record['city']}, OR {record['zip']}, USA"
        point = geocode(geolocator, cache, 'store:' + number, address)
        if point:
            distance = miles_between(center, point)
            locations[number] = (point, distance)
        else:
            logging.warning('Skipping store %s: unable to locate %s', number, address)
    filtered = defaultdict(list)
    for code, rows in inventory.items():
        for record in rows:
            info = locations.get(record['store_number'])
            if info and info[1] <= radius:
                record['coordinates'], record['distance_miles'] = info
                filtered[code].append(record)
        filtered[code].sort(key=lambda row: row['distance_miles'])
    logging.info('Geocoded %s/%s unique stocked stores; %s matching rows within %s miles',
                 len(locations), len(stores), sum(map(len, filtered.values())), radius)
    return filtered, center


def compose_message(inventory, zip_code, radius, report_dates):
    now = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lines = [now, f'Search ZIP Code: {zip_code}, Search Radius: {radius} miles',
             'Distance is approximate (straight-line, not driving distance).',
             f"OLCC report date(s): {', '.join(sorted(report_dates)) or 'Unknown'}", '']
    for code, name in ITEM_LIST:
        records = inventory.get(code, [])
        if not records:
            logging.info('No stock within radius for %s (%s)', name, code)
            continue
        logging.info('%s (%s): %s stores, %s bottles', name, code,
                     len(records), sum(row['quantity'] for row in records))
        lines.append('-------------------------')
        lines.append(f"{name} ({code}): ${records[0]['price']}")
        for row in records:
            lines.extend([
                f"    - {row['address']}, {row['city']}, OR {row['zip']}",
                f"      {row['phone']}",
                f"      Quantity: {row['quantity']} | Distance: {row['distance_miles']:.1f} miles",
            ])
        lines.append('')
    if not any(inventory.values()):
        lines.append('No tracked products with positive stock were found within this radius.')
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
    parser.add_argument('--local-csv', action='store_true',
                        help='Use previously downloaded data/DailyStoreInventory.csv')
    parser.add_argument('--no-email', action='store_true', help='Create results without sending email')
    args = parser.parse_args()
    if not re.fullmatch(r'\d{5}', args.zip):
        parser.error('ZIP code must be five digits')
    setup_logging()
    logging.info('Bot started: ZIP %s, radius %s miles', args.zip, args.radius)
    try:
        if not args.local_csv:
            download_csv()
        elif not CSV_FILE.is_file():
            raise FileNotFoundError(f'--local-csv specified but {CSV_FILE} is missing')
        inventory, dates = read_inventory()
        filtered, _center = enrich_with_distances(inventory, args.zip, args.radius)
        body = compose_message(filtered, args.zip, args.radius, dates)
        Path('last_email.txt').write_text(body, encoding='utf-8')
        logging.info('Email preview saved to last_email.txt')
        if not args.no_email:
            send_email(body, recipients_for_zip(args.zip), args.zip)
        logging.info('Bot completed successfully')
    except Exception:
        logging.exception('Bot failed')
        sys.exit(1)


if __name__ == '__main__':
    main()
