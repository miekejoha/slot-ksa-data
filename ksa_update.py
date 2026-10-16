"""Complete KSA snapshot. Run: python3 ksa_update.py [--reset-baseline].

Uses the site's own JetSmartFilters endpoint, without browser click races.
No output is changed until every overview and detail has been validated.
A checksum manifest distinguishes verified snapshots from legacy/incomplete data.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import threading
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT
URL = 'https://openovergokken.nl/kansspelwijzer/'
FILES = ('ksa_actueel.json', 'ksa_nieuw.json', 'ksa_vervallen.json')
MANIFEST = DATA / 'ksa_snapshot.json'
HTTP = threading.local()
TYPE = 'Exploitatie speelautomaat'
SENTINEL = 'Van Rhijn Speelautomaten B.V.'
MONTHS = 'januari februari maart april mei juni juli augustus september oktober november december'.split()


def clean(value):
    return ' '.join(value.split())


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def retry(operation, label):
    for attempt in range(1, 4):
        try:
            return operation()
        except Exception as error:
            if attempt == 3:
                raise RuntimeError(f'{label}: na 3 pogingen niet betrouwbaar leesbaar: {error}') from error
            print(f'{label}: poging {attempt}/3 mislukt: {error}', flush=True)
            time.sleep(attempt)


def request(method, url, **kwargs):
    if not hasattr(HTTP, 'session'):
        HTTP.session = requests.Session()
    response = HTTP.session.request(method, url, timeout=(15, 45), **kwargs)
    response.raise_for_status()
    require(urlparse(response.url).hostname == 'openovergokken.nl', 'Onverwachte redirect')
    return response


def settings():
    def fetch():
        html = request('GET', URL).text
        match = re.search(r'var JetSmartFilterSettings\s*=\s*({[^\n]+});', html)
        require(match, 'Paginering-instellingen ontbreken')
        return json.loads(match.group(1))
    return retry(fetch, 'KSA-overzicht')


def overview(config, number):
    provider = 'epro-loop-builder'
    payload = {'action': 'jet_smart_filters', 'provider': provider + '/default', 'paged': number}
    for section, source in [('defaults', 'queries'), ('settings', 'settings'), ('props', 'props')]:
        for key, value in config[source][provider]['default'].items():
            if isinstance(value, (dict, list)):
                require(not value, f'Onverwachte complexe instelling: {key}')
                continue
            payload[f'{section}[{key}]'] = str(value).lower() if isinstance(value, bool) else value
    def fetch():
        result = request('POST', config['ajaxurl'], data=payload).json()
        pagination = result['pagination']
        expected = config['props'][provider]['default']
        require(int(pagination['page']) == number, f'Verkeerde pagina: {pagination}')
        for key in ('found_posts', 'max_num_pages'):
            require(int(pagination[key]) == int(expected[key]), 'Overzicht veranderde tijdens ophalen')
        soup = BeautifulSoup(result['content'], 'html.parser')
        cards = soup.select('.e-loop-item')
        require(cards, f'Lege overzichtspagina {number}')
        records = []
        for card in cards:
            link = card.select_one('.elementor-widget-theme-post-title a')
            require(link and clean(link.get_text()), 'Naam/detail-link ontbreekt')
            href = link['href'].split('?')[0].rstrip('/') + '/'
            require(href.startswith(URL) and href != URL, f'Ongeldige detail-link: {href}')
            records.append({'naam': clean(link.get_text()), 'url': href})
        return records
    return retry(fetch, f'Overzichtspagina {number}')


def collect_overviews():
    config = settings()
    props = config['props']['epro-loop-builder']['default']
    total, pages = int(props['found_posts']), int(props['max_num_pages'])
    require(total > 0 and 0 < pages <= total, 'Ongeldige overzichtsaantallen')
    seen, records = set(), []
    size = None
    for number in range(1, pages + 1):
        batch = overview(config, number)
        if size is None:
            size = len(batch)
        expected = size if number < pages else total - size * (pages - 1)
        require(len(batch) == expected, f'Pagina {number}: {len(batch)} resultaten, verwacht {expected}')
        urls = [item['url'] for item in batch]
        require(len(set(urls)) == len(urls), f'Dubbele resultaten binnen pagina {number}')
        require(not seen.intersection(urls), f'Dubbele/overlappende overzichtspagina {number}; STOP')
        seen.update(urls)
        records.extend(batch)
        print(f'Overzicht {number}/{pages}: {len(batch)} unieke resultaten', flush=True)
    require(len(records) == total, f'Onvolledig overzicht: {len(records)}/{total}')
    require(any(r['naam'].casefold() == SENTINEL.casefold() for r in records), 'Van Rhijn ontbreekt in ruwe lijst')
    print(f'Ruwe lijst compleet: {total}; Van Rhijn aanwezig.', flush=True)
    return records


def date_key(value):
    day, month, year = value.lower().split()
    return date(int(year), MONTHS.index(month) + 1, int(day))


def parse_detail(html, basis):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.find('h1')
    require(heading and clean(heading.get_text()), 'Detailnaam ontbreekt')
    canonical = soup.select_one('link[rel=canonical]')
    require(canonical and canonical.get('href', '').rstrip('/') == basis['url'].rstrip('/'), 'Verkeerde detailpagina')
    # Cards may use a trading name; use the legal name from the detail page.
    basis = {**basis, 'naam': clean(heading.get_text())}
    fields = []
    labels = {'Nummer handelsregister', 'Vergunningtype', 'Geldig'}
    for widget in soup.select('.elementor-widget-text-editor'):
        label = clean(widget.get_text(' ', strip=True))
        if label in labels:
            sibling = widget.find_next_sibling('div')
            value = clean(sibling.get_text(' ', strip=True)) if sibling else ''
            if value in labels:
                value = ''
            fields.append((label, value))
    types = [v for k, v in fields if k == 'Vergunningtype']
    require(types and all(types), 'Vergunningtype ontbreekt; pagina onleesbaar')
    selected = []
    current_type = None
    for label, value in fields:
        if label == 'Vergunningtype':
            current_type = value
        elif label == 'Geldig' and current_type == TYPE:
            match = re.fullmatch(r'\d{1,2}\s+\w+\s+\d{4}\s*[ââ-]\s*(\d{1,2}\s+\w+\s+\d{4})', value)
            require(match, f'Ongeldige geldigheidsperiode: {value}')
            end = match.group(1)
            date_key(end)
            selected.append(end)
    if TYPE not in types:
        return None
    require(len(selected) == types.count(TYPE), 'Einddatum exploitatievergunning ontbreekt')
    kvks = {re.sub(r'[.\s]', '', v) for k, v in fields if k == 'Nummer handelsregister'}
    require(len(kvks) == 1, 'KVK ontbreekt of is niet eenduidig')
    kvk = kvks.pop()
    require(re.fullmatch(r'\d{7,8}', kvk), f'Ongeldig KVK: {kvk}')
    return {**basis, 'kvk': kvk.zfill(8), 'geldigTot': max(selected, key=date_key), 'vergunningType': TYPE}


def detail(basis):
    return retry(lambda: parse_detail(request('GET', basis['url']).text, basis), basis['naam'])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def previous(reset):
    if reset or not MANIFEST.exists():
        print('Schone nulmeting: oude/onbevestigde data en historie worden niet gebruikt.', flush=True)
        return None, [], []
    manifest = json.loads(MANIFEST.read_text())
    require(manifest.get('version') == 1, 'Onbekend snapshotformaat')
    result = []
    for name in FILES:
        raw = (DATA / name).read_bytes()
        require(digest(raw) == manifest['sha256'][name], f'{name} gewijzigd/onvolledig; gebruik bewust --reset-baseline voor nieuwe nulmeting')
        result.append(json.loads(raw))
    return result


def publish(outputs, raw_total):
    # Stage all files first. Commit manifest last: an interrupted write cannot
    # subsequently be mistaken for a verified baseline.
    staged = []
    payloads = {name: (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode() for name, value in zip(FILES, outputs)}
    manifest = {'version': 1, 'date': date.today().isoformat(), 'rawCount': raw_total,
                'sha256': {name: digest(raw) for name, raw in payloads.items()}}
    payloads[MANIFEST.name] = (json.dumps(manifest, indent=2) + '\n').encode()
    try:
        for name, raw in payloads.items():
            with tempfile.NamedTemporaryFile(dir=DATA, prefix='.ksa-', delete=False) as handle:
                staged.append((Path(handle.name), DATA / name))
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        for temporary, target in staged:
            os.replace(temporary, target)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reset-baseline', action='store_true')
    args = parser.parse_args()
    # Lock the script itself; no persistent lock/test files are necessary.
    with open(__file__, 'rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old, added, removed = previous(args.reset_baseline)
        raw = collect_overviews()
        by_kvk = {}
        with ThreadPoolExecutor(max_workers=16) as pool:
            for index, item in enumerate(pool.map(detail, raw), 1):
                if item:
                    kvk = item['kvk']
                    if kvk in by_kvk:
                        existing = by_kvk[kvk]
                        require(existing['naam'].casefold() == item['naam'].casefold(),
                                f'Conflicterende bedrijfsnamen bij KVK {kvk}')
                        print(f'Zelfde KVK samengevoegd: {kvk} ({item["naam"]})', flush=True)
                        if date_key(item['geldigTot']) > date_key(existing['geldigTot']):
                            by_kvk[kvk] = item
                    else:
                        by_kvk[kvk] = item
                if index % 40 == 0 or index == len(raw):
                    print(f'Details gecontroleerd: {index}/{len(raw)}', flush=True)
        current = sorted(by_kvk.values(), key=lambda item: item['naam'].casefold())
        require(any(r['naam'].casefold() == SENTINEL.casefold() for r in current), 'Van Rhijn ontbreekt in eindresultaat')
        if old is not None:
            old_map = {r['kvk']: r for r in old}
            today = date.today().isoformat()
            for keys, source, history in [(by_kvk.keys() - old_map.keys(), by_kvk, added),
                                          (old_map.keys() - by_kvk.keys(), old_map, removed)]:
                for kvk in sorted(keys):
                    history.append({key: source[kvk][key] for key in ('naam', 'kvk', 'geldigTot')} | {'datumConstatering': today})
        for history in (added, removed):
            history.sort(key=lambda r: (r['datumConstatering'], r['naam'].casefold()), reverse=True)
        publish((current, added, removed), len(raw))
        for name, expected in zip(FILES, (current, added, removed)):
            require(json.loads((DATA / name).read_text()) == expected, f'Opslagcontrole mislukt: {name}')
        print(f'KLAAR: Actueel {len(current)} / Nieuw {len(added)} / Vervallen {len(removed)}. Van Rhijn bevestigd.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'STOP: {error}', file=sys.stderr, flush=True)
        sys.exit(1)

