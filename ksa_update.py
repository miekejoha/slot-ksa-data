"""Complete KSA snapshot.

Run:

    python3 ksa_update.py

Uses the site's JetSmartFilters endpoint.

Existing history is preserved.

No data is published unless all checks succeed.

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

import threading

import time

from urllib.parse import urlparse

import certifi

import requests

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent

DATA = ROOT

URL = "https://openovergokken.nl/kansspelwijzer/"

TYPE = "Exploitatie speelautomaat"

SENTINEL = "Van Rhijn Speelautomaten B.V."

FILES = (

    "ksa_actueel.json",

    "ksa_nieuw.json",

    "ksa_vervallen.json",

)

MANIFEST = DATA / "ksa_snapshot.json"

HTTP = threading.local()

MONTHS = (

    "januari februari maart april mei juni juli augustus "

    "september oktober november december"

).split()

def clean(value):

    return " ".join(value.split())

def require(condition, message):

    if not condition:

        raise RuntimeError(message)

def retry(operation, label):

    for attempt in range(1, 4):

        try:

            return operation()

        except Exception as error:

            if attempt == 3:

                raise RuntimeError(

                    f"{label}: na 3 pogingen niet betrouwbaar "

                    f"leesbaar: {error}"

                ) from error

            print(

                f"{label}: poging {attempt}/3 mislukt: {error}",

                flush=True,

            )

            time.sleep(attempt)

def request(method, url, **kwargs):

    if not hasattr(HTTP, "session"):

        HTTP.session = requests.Session()

        # SSL-verificatie blijft verplicht.

        # Een eigen CA-bundel kan via de omgeving

        # REQUESTS_CA_BUNDLE worden ingesteld.

        HTTP.session.verify = os.environ.get(

            "REQUESTS_CA_BUNDLE",

            certifi.where(),

        )

    response = HTTP.session.request(

        method,

        url,

        timeout=(15, 45),

        **kwargs,

    )

    response.raise_for_status()

    require(

        urlparse(response.url).hostname == "openovergokken.nl",

        "Onverwachte redirect",

    )

    return response

def settings():

    def fetch():

        html = request("GET", URL).text

        match = re.search(

            r"var JetSmartFilterSettings\s*=\s*({[^\n]+});",

            html,

        )

        require(match, "Paginering-instellingen ontbreken")

        return json.loads(match.group(1))

    return retry(fetch, "KSA-overzicht")

def overview(config, number):

    provider = "epro-loop-builder"

    payload = {

        "action": "jet_smart_filters",

        "provider": provider + "/default",

        "paged": number,

    }

    for section, source in (

        ("defaults", "queries"),

        ("settings", "settings"),

        ("props", "props"),

    ):

        values = config[source][provider]["default"]

        for key, value in values.items():

            if isinstance(value, (dict, list)):

                require(

                    not value,

                    f"Onverwachte complexe instelling: {key}",

                )

                continue

            if isinstance(value, bool):

                value = str(value).lower()

            payload[f"{section}[{key}]"] = value

    def fetch():

        result = request(

            "POST",

            config["ajaxurl"],

            data=payload,

        ).json()

        pagination = result["pagination"]

        expected = config["props"][provider]["default"]

        require(

            int(pagination["page"]) == number,

            f"Verkeerde pagina: {pagination}",

        )

        for key in ("found_posts", "max_num_pages"):

            require(

                int(pagination[key]) == int(expected[key]),

                "Overzicht veranderde tijdens ophalen",

            )

        soup = BeautifulSoup(

            result["content"],

            "html.parser",

        )

        cards = soup.select(".e-loop-item")

        require(

            cards,

            f"Lege overzichtspagina {number}",

        )

        records = []

        for card in cards:

            link = card.select_one(

                ".elementor-widget-theme-post-title a"

            )

            require(

                link and clean(link.get_text()),

                "Naam/detail-link ontbreekt",

            )

            href = (

                link["href"].split("?")[0].rstrip("/") + "/"

            )

            require(

                href.startswith(URL) and href != URL,

                f"Ongeldige detail-link: {href}",

            )

            records.append({

                "naam": clean(link.get_text()),

                "url": href,

            })

        return records

    return retry(fetch, f"Overzichtspagina {number}")

def collect_overviews():

    config = settings()

    props = config["props"]["epro-loop-builder"]["default"]

    total = int(props["found_posts"])

    pages = int(props["max_num_pages"])

    require(

        total > 0 and 0 < pages <= total,

        "Ongeldige overzichtsaantallen",

    )

    seen = set()

    records = []

    size = None

    for number in range(1, pages + 1):

        batch = overview(config, number)

        if size is None:

            size = len(batch)

        expected = (

            size

            if number < pages

            else total - size * (pages - 1)

        )

        require(

            len(batch) == expected,

            f"Pagina {number}: {len(batch)} resultaten, "

            f"verwacht {expected}",

        )

        urls = [item["url"] for item in batch]

        require(

            len(set(urls)) == len(urls),

            f"Dubbele resultaten binnen pagina {number}",

        )

        require(

            not seen.intersection(urls),

            f"Dubbele overzichtspagina {number}; STOP",

        )

        seen.update(urls)

        records.extend(batch)

        print(

            f"Overzicht {number}/{pages}: "

            f"{len(batch)} unieke resultaten",

            flush=True,

        )

    require(

        len(records) == total,

        f"Onvolledig overzicht: {len(records)}/{total}",

    )

    require(

        any(

            item["naam"].casefold() == SENTINEL.casefold()

            for item in records

        ),

        "Van Rhijn ontbreekt in ruwe lijst",

    )

    print(

        f"Ruwe lijst compleet: {total}; "

        "Van Rhijn aanwezig.",

        flush=True,

    )

    return records

def date_key(value):

    day, month, year = value.lower().split()

    return date(

        int(year),

        MONTHS.index(month) + 1,

        int(day),

    )

def parse_detail(html, basis):

    soup = BeautifulSoup(html, "html.parser")

    heading = soup.find("h1")

    require(

        heading and clean(heading.get_text()),

        "Detailnaam ontbreekt",

    )

    canonical = soup.select_one("link[rel=canonical]")

    require(

        canonical

        and canonical.get("href", "").rstrip("/")

        == basis["url"].rstrip("/"),

        "Verkeerde detailpagina",

    )

    basis = {

        **basis,

        "naam": clean(heading.get_text()),

    }

    fields = []

    labels = {

        "Nummer handelsregister",

        "Vergunningtype",

        "Geldig",

    }

    for widget in soup.select(

        ".elementor-widget-text-editor"

    ):

        label = clean(

            widget.get_text(" ", strip=True)

        )

        if label in labels:

            sibling = widget.find_next_sibling("div")

            value = (

                clean(sibling.get_text(" ", strip=True))

                if sibling

                else ""

            )

            if value in labels:

                value = ""

            fields.append((label, value))

    types = [

        value

        for key, value in fields

        if key == "Vergunningtype"

    ]

    require(

        types and all(types),

        "Vergunningtype ontbreekt",

    )

    selected = []

    current_type = None

    for label, value in fields:

        if label == "Vergunningtype":

            current_type = value

        elif label == "Geldig" and current_type == TYPE:

            match = re.fullmatch(

                r"\d{1,2}\s+\w+\s+\d{4}"

                r"\s*[\u2013\u2014-]\s*"

                r"(\d{1,2}\s+\w+\s+\d{4})",

                value,

            )

            require(

                match,

                f"Ongeldige geldigheidsperiode: {value}",

            )

            end = match.group(1)

            date_key(end)

            selected.append(end)

    if TYPE not in types:

        return None

    require(

        len(selected) == types.count(TYPE),

        "Einddatum exploitatievergunning ontbreekt",

    )

    kvks = {

        re.sub(r"[.\s]", "", value)

        for key, value in fields

        if key == "Nummer handelsregister"

    }

    require(

        len(kvks) == 1,

        "KVK ontbreekt of is niet eenduidig",

    )

    kvk = kvks.pop()

    require(

        re.fullmatch(r"\d{7,8}", kvk),

        f"Ongeldig KVK: {kvk}",

    )

    return {

        **basis,

        "kvk": kvk.zfill(8),

        "geldigTot": max(selected, key=date_key),

        "vergunningType": TYPE,

    }

def detail(basis):

    return retry(

        lambda: parse_detail(

            request("GET", basis["url"]).text,

            basis,

        ),

        basis["naam"],

    )

def digest(data):

    return hashlib.sha256(data).hexdigest()

def previous(reset):

    if reset:

        raise RuntimeError(

            "Automatische nulmeting is uitgeschakeld "

            "om bestaande historie te beschermen."

        )

    existing = [

        name

        for name in FILES

        if (DATA / name).exists()

    ]

    if not MANIFEST.exists():

        require(

            not existing,

            "Bestaande KSA-gegevens zonder gecontroleerd "

            "snapshot. Er wordt niets overschreven.",

        )

        print(

            "Eerste nulmeting: geen bestaande gegevens.",

            flush=True,

        )

        return None, [], []

    manifest = json.loads(

        MANIFEST.read_text(encoding="utf-8")

    )

    require(

        manifest.get("version") == 1,

        "Onbekend snapshotformaat",

    )

    result = []

    for name in FILES:

        path = DATA / name

        require(

            path.exists(),

            f"Ontbrekend historiebestand: {name}",

        )

        raw = path.read_bytes()

        require(

            digest(raw) == manifest["sha256"][name],

            f"{name} wijkt af van de gecontroleerde "

            "nulmeting. STOP.",

        )

        result.append(json.loads(raw))

    current, added, removed = result

    require(

        isinstance(current, list)

        and isinstance(added, list)

        and isinstance(removed, list),

        "Ongeldig formaat bestaande KSA-gegevens",

    )

    print(

        f"Bestaande historie geladen: "

        f"{len(current)} actueel, "

        f"{len(added)} nieuw, "

        f"{len(removed)} vervallen.",

        flush=True,

    )

    return current, added, removed

def publish(outputs, raw_total):

    payloads = {

        name: (

            json.dumps(

                value,

                ensure_ascii=False,

                indent=2,

            ) + "\n"

        ).encode("utf-8")

        for name, value in zip(FILES, outputs)

    }

    manifest = {

        "version": 1,

        "date": date.today().isoformat(),

        "rawCount": raw_total,

        "sha256": {

            name: digest(raw)

            for name, raw in payloads.items()

        },

    }

    payloads[MANIFEST.name] = (

        json.dumps(manifest, indent=2) + "\n"

    ).encode("utf-8")

    staged = []

    try:

        for name, raw in payloads.items():

            with tempfile.NamedTemporaryFile(

                dir=DATA,

                prefix=".ksa-",

                delete=False,

            ) as handle:

                staged.append(

                    (Path(handle.name), DATA / name)

                )

                handle.write(raw)

                handle.flush()

                os.fsync(handle.fileno())

        # Manifest wordt als laatste geplaatst.

        for temporary, target in staged:

            os.replace(temporary, target)

    finally:

        for temporary, _ in staged:

            temporary.unlink(missing_ok=True)

def main():

    parser = argparse.ArgumentParser(

        description=__doc__

    )

    parser.add_argument(

        "--reset-baseline",

        action="store_true",

    )

    args = parser.parse_args()

    with open(__file__, "rb") as lock:

        fcntl.flock(

            lock,

            fcntl.LOCK_EX | fcntl.LOCK_NB,

        )

        old, added, removed = previous(

            args.reset_baseline

        )

        raw = collect_overviews()

        by_kvk = {}

        with ThreadPoolExecutor(

            max_workers=16

        ) as pool:

            for index, item in enumerate(

                pool.map(detail, raw),

                1,

            ):

                if item:

                    kvk = item["kvk"]

                    if kvk in by_kvk:

                        existing = by_kvk[kvk]

                        require(

                            existing["naam"].casefold()

                            == item["naam"].casefold(),

                            f"Conflicterende namen: {kvk}",

                        )

                        if (

                            date_key(item["geldigTot"])

                            > date_key(

                                existing["geldigTot"]

                            )

                        ):

                            by_kvk[kvk] = item

                    else:

                        by_kvk[kvk] = item

                if (

                    index % 40 == 0

                    or index == len(raw)

                ):

                    print(

                        f"Details gecontroleerd: "

                        f"{index}/{len(raw)}",

                        flush=True,

                    )

        current = sorted(

            by_kvk.values(),

            key=lambda item: item["naam"].casefold(),

        )

        require(

            any(

                item["naam"].casefold()

                == SENTINEL.casefold()

                for item in current

            ),

            "Van Rhijn ontbreekt in eindresultaat",

        )

        if old is not None:

            old_map = {

                item["kvk"]: item

                for item in old

            }

            today = date.today().isoformat()

            changes = (

                (

                    by_kvk.keys() - old_map.keys(),

                    by_kvk,

                    added,

                ),

                (

                    old_map.keys() - by_kvk.keys(),

                    old_map,

                    removed,

                ),

            )

            for keys, source, history in changes:

                for kvk in sorted(keys):

                    history.append({

                        key: source[kvk][key]

                        for key in (

                            "naam",

                            "kvk",

                            "geldigTot",

                        )

                    } | {

                        "datumConstatering": today

                    })

        for history in (added, removed):

            history.sort(

                key=lambda item: (

                    item["datumConstatering"],

                    item["naam"].casefold(),

                ),

                reverse=True,

            )

        publish(

            (current, added, removed),

            len(raw),

        )

        print(

            f"KLAAR: Actueel {len(current)} / "

            f"Nieuw {len(added)} / "

            f"Vervallen {len(removed)}. "

            "Van Rhijn bevestigd.",

            flush=True,

        )

if __name__ == "__main__":

    try:

        main()

    except Exception as error:

        print(

            f"STOP: {error}",

            file=sys.stderr,

            flush=True,

        )

        sys.exit(1)