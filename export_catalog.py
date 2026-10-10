#!/usr/bin/env python3
"""Export jazz_albums.db to docs/catalog.json for the offline web app."""

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = PROJECT_DIR / "jazz_albums.db"
DEFAULT_OUTPUT = PROJECT_DIR / "docs" / "catalog.json"
CATALOG_FORMAT_VERSION = 1


def grouped_rows(connection, query):
    groups = defaultdict(list)
    for key, *values in connection.execute(query):
        groups[key].append(values)
    return groups


def export_catalog(database_file, output_file):
    with sqlite3.connect(database_file) as connection:
        albums = connection.execute(
            """
            SELECT id, musicbrainz_id, title, original_release_date, original_release_year
            FROM albums
            """
        ).fetchall()
        artists = grouped_rows(
            connection,
            """
            SELECT aa.album_id, a.name
            FROM album_artists AS aa
            JOIN artists AS a ON a.id = aa.artist_id
            ORDER BY aa.album_id, aa.credit_order
            """,
        )
        genres = grouped_rows(
            connection,
            """
            SELECT ag.album_id, g.name
            FROM album_genres AS ag
            JOIN genres AS g ON g.id = ag.genre_id
            ORDER BY ag.album_id, g.name
            """,
        )
        personnel = grouped_rows(
            connection,
            """
            SELECT p.album_id, a.name, p.role
            FROM personnel AS p
            JOIN artists AS a ON a.id = p.artist_id
            ORDER BY p.album_id, a.name, p.role
            """,
        )
        tracks = grouped_rows(
            connection,
            """
            SELECT r.album_id, t.disc_number, t.track_number, t.title, t.duration_ms
            FROM tracks AS t
            JOIN releases AS r ON r.id = t.release_id
            ORDER BY r.album_id, t.disc_number, t.position
            """,
        )

    catalog_albums = []
    for album_id, musicbrainz_id, title, date, year in albums:
        catalog_albums.append(
            {
                "id": musicbrainz_id,
                "title": title,
                "date": date,
                "year": year,
                "artists": [name for (name,) in artists[album_id]],
                "genres": [name for (name,) in genres[album_id]],
                "personnel": [
                    {"name": name, "role": role}
                    for name, role in personnel[album_id]
                ],
                "tracks": [
                    {
                        "disc": disc,
                        "number": number,
                        "title": track_title,
                        "durationMs": duration_ms,
                    }
                    for disc, number, track_title, duration_ms in tracks[album_id]
                ],
            }
        )

    catalog = {
        "formatVersion": CATALOG_FORMAT_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "albumCount": len(catalog_albums),
        "albums": catalog_albums,
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return len(catalog_albums)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export the jazz archive for the offline web app."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    if not args.database.exists():
        print(f"Database not found: {args.database}")
        return 1

    count = export_catalog(args.database, args.output)
    print(f"Exported {count} albums to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
