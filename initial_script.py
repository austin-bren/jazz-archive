
import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import requests

BASE_URL = "https://musicbrainz.org/ws/2"
DATABASE_FILE = Path("jazz_albums.db")

DATABASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    musicbrainz_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    original_release_date TEXT,
    original_release_year INTEGER,
    recording_year_start INTEGER,
    recording_year_end INTEGER,
    source_url TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY,
    musicbrainz_id TEXT UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS album_artists (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id),
    credit_order INTEGER NOT NULL,
    PRIMARY KEY (album_id, artist_id)
);

CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY,
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    musicbrainz_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    release_date TEXT,
    release_year INTEGER,
    country TEXT,
    status TEXT,
    barcode TEXT,
    packaging TEXT
);

CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY,
    musicbrainz_id TEXT UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS release_labels (
    release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id),
    catalog_number TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (release_id, label_id, catalog_number)
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    musicbrainz_recording_id TEXT,
    disc_number INTEGER,
    track_number TEXT,
    position INTEGER,
    title TEXT NOT NULL,
    duration_ms INTEGER,
    UNIQUE (release_id, disc_number, position)
);

CREATE TABLE IF NOT EXISTS track_recording_dates (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    recording_date TEXT NOT NULL,
    PRIMARY KEY (track_id, recording_date)
);

CREATE TABLE IF NOT EXISTS track_artists (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id),
    credit_order INTEGER NOT NULL,
    PRIMARY KEY (track_id, artist_id)
);

CREATE TABLE IF NOT EXISTS personnel (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id),
    role TEXT NOT NULL,
    PRIMARY KEY (album_id, artist_id, role)
);

CREATE TABLE IF NOT EXISTS genres (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS album_genres (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id),
    PRIMARY KEY (album_id, genre_id)
);

CREATE INDEX IF NOT EXISTS idx_tracks_release_id ON tracks(release_id);
CREATE INDEX IF NOT EXISTS idx_releases_album_id ON releases(album_id);
PRAGMA user_version = 1;
"""

# Use a meaningful application name and contact address.
MUSICBRAINZ_CONTACT = os.environ.get("MUSICBRAINZ_CONTACT")
HEADERS = {
    "User-Agent": (
        "PersonalJazzArchive/0.2 "
        f"({MUSICBRAINZ_CONTACT or 'contact-not-configured'})"
    )
}

session = requests.Session()
session.headers.update(HEADERS)

_last_request = 0.0
_request_count = 0
_request_budget = None


class RequestBudgetExceeded(RuntimeError):
    pass


def connect_database(path=DATABASE_FILE):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(DATABASE_SCHEMA)
    return connection


def mb_get(endpoint, params=None):
    """Make a rate-limited MusicBrainz API request."""
    global _last_request, _request_count

    if _request_budget is not None and _request_count >= _request_budget:
        raise RequestBudgetExceeded(
            f"Request budget of {_request_budget} was reached."
        )

    for attempt in range(3):
        elapsed = time.monotonic() - _last_request
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)

        response = session.get(
            f"{BASE_URL}/{endpoint}",
            params={**(params or {}), "fmt": "json"},
            timeout=30,
        )
        _last_request = time.monotonic()
        _request_count += 1

        if response.status_code not in {429, 502, 503, 504}:
            response.raise_for_status()
            return response.json()

        if attempt == 2:
            response.raise_for_status()

        retry_after = response.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else 2 ** (attempt + 1)
        time.sleep(min(delay, 60))

        if _request_budget is not None and _request_count >= _request_budget:
            raise RequestBudgetExceeded(
                f"Request budget of {_request_budget} was reached."
            )

    raise RuntimeError("MusicBrainz request failed without a response.")


def set_request_budget(limit):
    global _request_budget, _request_count
    _request_budget = limit
    _request_count = 0


def search_albums(query, limit=10):
    """Search album/release-group records."""
    data = mb_get(
        "release-group",
        {
            "query": f"{query} AND primarytype:album",
            "limit": limit,
        },
    )
    return data.get("release-groups", [])


def search_artists(query, limit=10):
    escaped_query = query.replace("\\", "\\\\").replace('"', '\\"')
    data = mb_get(
        "artist",
        {
            "query": f'artist:"{escaped_query}"',
            "limit": limit,
        },
    )
    return data.get("artists", [])


def find_exact_artists(name):
    results = search_artists(name)
    return [
        artist
        for artist in results
        if artist.get("name", "").casefold() == name.casefold()
    ]


def parse_artist_spec(value):
    name, separator, possible_id = value.rpartition("|")
    if not separator:
        return value.strip(), None

    name = name.strip()
    possible_id = possible_id.strip()
    try:
        artist_id = str(uuid.UUID(possible_id))
    except ValueError as error:
        raise ValueError(
            f'Invalid MusicBrainz artist ID in "{value}".'
        ) from error
    if not name:
        raise ValueError("Artist name cannot be empty.")
    return name, artist_id


def resolve_artists(spec):
    name, artist_id = parse_artist_spec(spec)
    if not artist_id:
        return find_exact_artists(name)

    artist = mb_get(f"artist/{artist_id}")
    if artist.get("name", "").casefold() != name.casefold():
        raise ValueError(
            f'Artist ID {artist_id} belongs to "{artist.get("name")}", '
            f'not "{name}".'
        )
    return [artist]


def browse_artist_albums(artist_id):
    albums = []
    offset = 0
    page_size = 100

    while True:
        data = mb_get(
            "release-group",
            {
                "artist": artist_id,
                "type": "album",
                "release-group-status": "website-default",
                "inc": "artist-credits",
                "limit": page_size,
                "offset": offset,
            },
        )
        page = data.get("release-groups", [])
        albums.extend(page)
        offset += len(page)
        total = data.get("release-group-count")
        if not page or len(page) < page_size or (
            total is not None and offset >= total
        ):
            break

    return albums


def artist_credit_text(credits):
    return "".join(
        f"{credit.get('name') or credit.get('artist', {}).get('name', '')}"
        f"{credit.get('joinphrase', '')}"
        for credit in credits
    )


def artists_from_credits(credits):
    artists = []
    seen = set()
    for credit in credits:
        artist = credit.get("artist", {})
        name = credit.get("name") or artist.get("name")
        key = artist.get("id") or name
        if not name or key in seen:
            continue
        seen.add(key)
        artists.append({
            "name": name,
            "musicbrainz_id": artist.get("id"),
        })
    return artists


def choose_album(results):
    if not results:
        print("No results found.")
        return None

    for i, album in enumerate(results, start=1):
        artist_credit = artist_credit_text(album.get("artist-credit", []))
        print(
            f"{i}. {album.get('title', 'Unknown')} "
            f"- {artist_credit or 'Unknown artist'} "
            f"({album.get('first-release-date', 'Unknown year')})"
        )

    while True:
        choice = input("Choose a number, or Enter to skip: ").strip()
        if not choice:
            return None

        if choice.isdigit() and 1 <= int(choice) <= len(results):
            return results[int(choice) - 1]

        print("Invalid selection.")


def get_release_details(release_group_id):
    """Fetch releases, labels, and track listings."""
    group = mb_get(
        f"release-group/{release_group_id}",
        {"inc": "releases+genres+artist-credits"},
    )

    releases = group.get("releases", [])
    if not releases:
        return group, None

    # Prefer the earliest dated release with a track listing.
    releases.sort(
        key=lambda r: (
            r.get("status") != "Official",
            not bool(r.get("date")),
            r.get("date", "9999"),
        )
    )

    # Check releases individually until we find track data.
    for release in releases[:10]:
        details = mb_get(
            f"release/{release['id']}",
            {
                "inc": (
                    "recordings+labels+artist-credits+recording-level-rels+"
                    "artist-rels+place-rels"
                )
            },
        )

        tracks = []
        personnel = {}
        for medium in details.get("media", []):
            for track in medium.get("tracks", []):
                recording = track.get("recording", {})
                relations = recording.get("relations", [])
                recording_dates = sorted({
                    date
                    for relation in relations
                    if relation.get("type") in {
                        "conductor",
                        "instrument",
                        "performer",
                        "performing orchestra",
                        "recorded at",
                        "vocal",
                    }
                    for date in (relation.get("begin"), relation.get("end"))
                    if date
                })
                track_number = track.get("number") or str(
                    track.get("position", "")
                )
                tracks.append({
                    "disc": medium.get("position"),
                    "number": track_number,
                    "position": track.get("position"),
                    "title": track.get("title") or recording.get("title"),
                    "duration_ms": (
                        track.get("length") or recording.get("length")
                    ),
                    "artists": artists_from_credits(
                        recording.get("artist-credit", [])
                    ),
                    "recording_dates": recording_dates,
                    "recording_id": recording.get("id"),
                })

                for relation in relations:
                    artist = relation.get("artist")
                    if not artist or not artist.get("name"):
                        continue
                    key = artist.get("id") or artist["name"]
                    person = personnel.setdefault(key, {
                        "name": artist["name"],
                        "musicbrainz_id": artist.get("id"),
                        "roles": set(),
                    })
                    roles = relation.get("attributes") or [
                        relation.get("type")
                    ]
                    person["roles"].update(role for role in roles if role)

        if tracks:
            labels = [
                {
                    "name": item.get("label", {}).get("name"),
                    "musicbrainz_id": item.get("label", {}).get("id"),
                    "catalog_number": item.get("catalog-number"),
                }
                for item in details.get("label-info", [])
                if item.get("label")
            ]

            details["_tracks"] = tracks
            details["_labels"] = labels
            details["_personnel"] = sorted(
                (
                    {
                        **person,
                        "roles": sorted(person["roles"]),
                    }
                    for person in personnel.values()
                ),
                key=lambda person: person["name"],
            )
            return group, details

    return group, None


def year_from_date(value):
    if value and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return None


def build_album_record(group, release):
    recording_dates = sorted({
        date
        for track in release["_tracks"]
        for date in track["recording_dates"]
    })
    recording_years = sorted({
        year
        for date in recording_dates
        if (year := year_from_date(date)) is not None
    })
    genres = sorted(
        group.get("genres", []),
        key=lambda genre: -genre.get("count", 0),
    )

    return {
        "title": group.get("title"),
        "artists": artists_from_credits(group.get("artist-credit", [])),
        "recording_dates": recording_dates,
        "recording_years": recording_years,
        "original_release_date": group.get("first-release-date"),
        "original_release_year": year_from_date(
            group.get("first-release-date")
        ),
        "release": {
            "title": release.get("title"),
            "date": release.get("date"),
            "year": year_from_date(release.get("date")),
            "country": release.get("country"),
            "status": release.get("status"),
            "barcode": release.get("barcode"),
            "packaging": release.get("packaging"),
            "labels": release["_labels"],
            "musicbrainz_id": release["id"],
        },
        "tracks": release["_tracks"],
        "personnel": release["_personnel"],
        "genres": [genre["name"] for genre in genres],
        "musicbrainz_id": group["id"],
        "source": "MusicBrainz",
        "source_url": (
            f"https://musicbrainz.org/release-group/{group['id']}"
        ),
    }


def get_or_create_artist(connection, artist):
    musicbrainz_id = artist.get("musicbrainz_id")
    if musicbrainz_id:
        connection.execute(
            """
            INSERT INTO artists (musicbrainz_id, name) VALUES (?, ?)
            ON CONFLICT (musicbrainz_id) DO UPDATE SET name = excluded.name
            """,
            (musicbrainz_id, artist["name"]),
        )
        row = connection.execute(
            "SELECT id FROM artists WHERE musicbrainz_id = ?",
            (musicbrainz_id,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT id FROM artists WHERE name = ? LIMIT 1",
            (artist["name"],),
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                "INSERT INTO artists (name) VALUES (?)",
                (artist["name"],),
            )
            return cursor.lastrowid
    return row["id"]


def get_or_create_label(connection, label):
    musicbrainz_id = label.get("musicbrainz_id")
    if musicbrainz_id:
        connection.execute(
            """
            INSERT INTO labels (musicbrainz_id, name) VALUES (?, ?)
            ON CONFLICT (musicbrainz_id) DO UPDATE SET name = excluded.name
            """,
            (musicbrainz_id, label["name"]),
        )
        row = connection.execute(
            "SELECT id FROM labels WHERE musicbrainz_id = ?",
            (musicbrainz_id,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT id FROM labels WHERE name = ? LIMIT 1",
            (label["name"],),
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                "INSERT INTO labels (name) VALUES (?)",
                (label["name"],),
            )
            return cursor.lastrowid
    return row["id"]


def save_album(album, database_file=DATABASE_FILE):
    with connect_database(database_file) as connection:
        duplicate = connection.execute(
            "SELECT 1 FROM albums WHERE musicbrainz_id = ?",
            (album["musicbrainz_id"],),
        ).fetchone()
        if duplicate:
            print("This album is already in your database.")
            return False

        recording_years = album["recording_years"]
        cursor = connection.execute(
            """
            INSERT INTO albums (
                musicbrainz_id, title, original_release_date,
                original_release_year, recording_year_start,
                recording_year_end, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                album["musicbrainz_id"],
                album["title"],
                album["original_release_date"],
                album["original_release_year"],
                min(recording_years) if recording_years else None,
                max(recording_years) if recording_years else None,
                album["source_url"],
            ),
        )
        album_id = cursor.lastrowid

        for credit_order, artist in enumerate(album["artists"]):
            artist_id = get_or_create_artist(connection, artist)
            connection.execute(
                """
                INSERT INTO album_artists (album_id, artist_id, credit_order)
                VALUES (?, ?, ?)
                """,
                (album_id, artist_id, credit_order),
            )

        release = album["release"]
        cursor = connection.execute(
            """
            INSERT INTO releases (
                album_id, musicbrainz_id, title, release_date, release_year,
                country, status, barcode, packaging
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                album_id,
                release["musicbrainz_id"],
                release["title"],
                release["date"],
                release["year"],
                release["country"],
                release["status"],
                release["barcode"],
                release["packaging"],
            ),
        )
        release_id = cursor.lastrowid

        for label in release["labels"]:
            label_id = get_or_create_label(connection, label)
            connection.execute(
                """
                INSERT INTO release_labels (
                    release_id, label_id, catalog_number
                ) VALUES (?, ?, ?)
                """,
                (release_id, label_id, label.get("catalog_number") or ""),
            )

        for track in album["tracks"]:
            cursor = connection.execute(
                """
                INSERT INTO tracks (
                    release_id, musicbrainz_recording_id, disc_number,
                    track_number, position, title, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    release_id,
                    track["recording_id"],
                    track["disc"],
                    track["number"],
                    track["position"],
                    track["title"],
                    track["duration_ms"],
                ),
            )
            track_id = cursor.lastrowid
            for recording_date in track["recording_dates"]:
                connection.execute(
                    """
                    INSERT INTO track_recording_dates (track_id, recording_date)
                    VALUES (?, ?)
                    """,
                    (track_id, recording_date),
                )
            for credit_order, artist in enumerate(track["artists"]):
                artist_id = get_or_create_artist(connection, artist)
                connection.execute(
                    """
                    INSERT INTO track_artists (track_id, artist_id, credit_order)
                    VALUES (?, ?, ?)
                    """,
                    (track_id, artist_id, credit_order),
                )

        for person in album["personnel"]:
            artist_id = get_or_create_artist(connection, person)
            for role in person["roles"]:
                connection.execute(
                    """
                    INSERT INTO personnel (album_id, artist_id, role)
                    VALUES (?, ?, ?)
                    """,
                    (album_id, artist_id, role),
                )

        for genre_name in album["genres"]:
            connection.execute(
                "INSERT OR IGNORE INTO genres (name) VALUES (?)",
                (genre_name,),
            )
            genre_id = connection.execute(
                "SELECT id FROM genres WHERE name = ?",
                (genre_name,),
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO album_genres (album_id, genre_id) VALUES (?, ?)
                """,
                (album_id, genre_id),
            )

    print(f"Saved to {database_file.resolve()}")
    return True


def existing_album_ids(database_file=DATABASE_FILE):
    with connect_database(database_file) as connection:
        return {
            row["musicbrainz_id"]
            for row in connection.execute("SELECT musicbrainz_id FROM albums")
        }


def import_resolved_artist(artist, albums, database_file=DATABASE_FILE):
    albums.sort(
        key=lambda album: (
            album.get("first-release-date") or "9999",
            album.get("title") or "",
        )
    )
    existing_ids = existing_album_ids(database_file)
    print(
        f"Found {len(albums)} album release groups credited to "
        f"{artist['name']}."
    )

    saved = 0
    already_present = 0
    missing_tracks = 0
    failed = 0
    budget_reached = False

    for index, album_summary in enumerate(albums, start=1):
        album_id = album_summary["id"]
        title = album_summary.get("title") or "Unknown title"
        date = album_summary.get("first-release-date") or "unknown date"
        if album_id in existing_ids:
            already_present += 1
            print(f"[{index}/{len(albums)}] Already saved: {title} ({date})")
            continue

        print(f"[{index}/{len(albums)}] Importing: {title} ({date})")
        try:
            group, release = get_release_details(album_id)
        except RequestBudgetExceeded as error:
            print(f"Stopped safely: {error}")
            budget_reached = True
            break
        except requests.RequestException as error:
            print(f"  Request failed: {error}", file=sys.stderr)
            failed += 1
            continue

        if not release:
            print("  Skipped: no release with a track listing.")
            missing_tracks += 1
            continue

        try:
            if save_album(build_album_record(group, release), database_file):
                saved += 1
                existing_ids.add(album_id)
        except sqlite3.Error as error:
            print(f"  Database error: {error}", file=sys.stderr)
            failed += 1

    print(
        "\nImport summary: "
        f"{saved} saved, {already_present} already present, "
        f"{missing_tracks} without tracks, {failed} failed, "
        f"{_request_count} API requests."
    )
    return 2 if budget_reached else (1 if failed else 0)


def import_artist_discography(artist_spec, database_file=DATABASE_FILE):
    try:
        artists = resolve_artists(artist_spec)
    except ValueError as error:
        print(f"Invalid artist: {error}", file=sys.stderr)
        return 1
    except RequestBudgetExceeded as error:
        print(f"Stopped safely: {error}")
        return 2
    except requests.RequestException as error:
        print(f"Artist lookup failed: {error}", file=sys.stderr)
        return 1

    if not artists:
        name, _ = parse_artist_spec(artist_spec)
        print(f'No exact MusicBrainz artist match for "{name}".')
        return 1

    if len(artists) > 1:
        print(f"Found {len(artists)} exact-name artists; importing all:")
        for artist in artists:
            note = artist.get("disambiguation") or "no disambiguation"
            print(f"  {artist['id']} - {note}")

    result = 0
    for artist in artists:
        try:
            albums = browse_artist_albums(artist["id"])
        except RequestBudgetExceeded as error:
            print(f"Stopped safely: {error}")
            return 2
        except requests.RequestException as error:
            print(f"Artist lookup failed: {error}", file=sys.stderr)
            result = 1
            continue

        artist_result = import_resolved_artist(
            artist,
            albums,
            database_file,
        )
        if artist_result == 2:
            return 2
        if artist_result:
            result = 1
    return result


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def collect_artist_specs(direct_artists, artist_files):
    specs = list(direct_artists)
    for artist_file in artist_files:
        with artist_file.open(encoding="utf-8") as file:
            specs.extend(
                line.strip()
                for line in file
                if line.strip() and not line.lstrip().startswith("#")
            )

    unique_specs = []
    seen = set()
    for spec in specs:
        key = spec.casefold()
        if key not in seen:
            seen.add(key)
            unique_specs.append(spec)
    return unique_specs


def build_argument_parser():
    parser = argparse.ArgumentParser(
        description="Collect jazz album metadata from MusicBrainz."
    )
    parser.add_argument(
        "--artist",
        action="append",
        default=[],
        help="import every album release group credited to this exact artist",
    )
    parser.add_argument(
        "--artist-file",
        action="append",
        default=[],
        type=Path,
        help="read artist names from a file, one per line",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DATABASE_FILE,
        help=f"SQLite database path (default: {DATABASE_FILE})",
    )
    parser.add_argument(
        "--request-budget",
        type=positive_integer,
        default=250,
        help="maximum API requests for this run (default: 250)",
    )
    return parser


def main(argv=None):
    args = build_argument_parser().parse_args(argv)
    if not MUSICBRAINZ_CONTACT:
        print(
            "Set MUSICBRAINZ_CONTACT to your email address or website before "
            "using the MusicBrainz API."
        )
        return 1

    set_request_budget(args.request_budget)
    try:
        artist_specs = collect_artist_specs(args.artist, args.artist_file)
    except OSError as error:
        print(f"Could not read artist file: {error}", file=sys.stderr)
        return 1

    if artist_specs:
        result = 0
        for index, artist_spec in enumerate(artist_specs, start=1):
            print(f"\n=== Artist {index}/{len(artist_specs)}: {artist_spec} ===")
            artist_result = import_artist_discography(
                artist_spec,
                args.database,
            )
            if artist_result == 2:
                return 2
            if artist_result:
                result = 1
        return result

    query = input("Search for a jazz album: ").strip()
    if not query:
        return 0

    try:
        results = search_albums(query)
    except (RequestBudgetExceeded, requests.RequestException) as error:
        print(f"MusicBrainz request failed: {error}", file=sys.stderr)
        return 1
    selected = choose_album(results)

    if not selected:
        return 0

    try:
        group, release = get_release_details(selected["id"])
    except (RequestBudgetExceeded, requests.RequestException) as error:
        print(f"MusicBrainz request failed: {error}", file=sys.stderr)
        return 1
    if not release:
        print("No release with a track listing was found.")
        return 1

    album = build_album_record(group, release)

    print("\nSelected album:")
    print(json.dumps(album, indent=2, ensure_ascii=False))

    if input("\nSave this album? [y/N] ").lower() == "y":
        save_album(album, args.database)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())