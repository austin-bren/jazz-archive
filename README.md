# Personal Jazz Archive

This script searches [MusicBrainz](https://musicbrainz.org/) and saves album metadata to a normalized SQLite database. SQLite is the primary format because it supports relationships, duplicate protection, indexed queries, and eventual use from an iOS application.

## Setup

```sh
conda activate basic
pip install -r requirements.txt
export MUSICBRAINZ_CONTACT="your-email@example.com"
python initial_script.py
```

MusicBrainz requires a real email address or website in the User-Agent. The script respects its one-request-per-second limit.


Import a short list by repeating `--artist`:

```sh
python initial_script.py \
	--artist "Chet Baker" \
	--artist "Miles Davis" \
	--artist "Freddie Hubbard" \
	--request-budget 250
```

For a longer list, create a text file with one artist per line. Blank lines and lines beginning with `#` are ignored:

```text
# artists.txt
Chet Baker
Miles Davis
Freddie Hubbard
Lee Morgan
```

Then import it with:

```sh
python initial_script.py --artist-file artists.txt --request-budget 250
```

The artists currently listed under "To Complete" are already available in `artists_to_import.txt`:

```sh
python initial_script.py --artist-file artists_to_import.txt --request-budget 250
```

The request budget is shared by the complete batch. If it is reached, rerun the same command: completed albums are skipped before detail requests, and importing resumes safely.

If multiple MusicBrainz artists have exactly the same name, the importer processes all of them and prints their IDs. To select only one, use `Name | MusicBrainz-ID` as an `--artist` value or a line in the file:

```text
Avishai Cohen | a9adebe6-3436-4746-bc76-3623e66f6e76
```

That ID is Avishai Cohen the trumpeter; `17d78170-7194-4603-8dbb-ca1108430882` is the bassist.
The database is created as `jazz_albums.db` in the current directory. Search results are interactive; the script currently picks the earliest official release with a track listing as the representative edition.

## Import an artist discography

Import every album release group credited to an exact artist name:

```sh
python initial_script.py --artist "Kenny Dorham"
```

The artist importer:

- waits at least 1.1 seconds between MusicBrainz requests;
- stops after 250 API requests by default;
- retries temporary `429`, `502`, `503`, and `504` responses with backoff;
- saves each completed album immediately;
- skips MusicBrainz album IDs already in SQLite; and
- can be rerun to resume safely after an interruption or request-budget stop.

Set a smaller request budget for a short batch:

```sh
python initial_script.py --artist "Kenny Dorham" --request-budget 50
```

```
export MUSICBRAINZ_CONTACT="hasbren@gmail.com"
python initial_script.py --artist "Joe Henderson" --request-budget 250
```

"Every album" means MusicBrainz album release groups where the artist is part of the album credit. It does not include every album on which the artist appears only as a sideman. Sideman appearances can be added as a separate import mode later because they require broader track-level matching and more API work.

## Data model

The database separates albums, releases, artists, labels, tracks, personnel, genres, and recording dates. MusicBrainz IDs are retained as stable external identifiers, and duplicate albums are rejected.

Useful checks with the SQLite command-line tool:

```sh
sqlite3 jazz_albums.db ".tables"
sqlite3 jazz_albums.db "SELECT title, original_release_year FROM albums;"
sqlite3 jazz_albums.db "SELECT name, role FROM personnel JOIN artists ON artists.id = personnel.artist_id;"
```

The schema version is stored in `PRAGMA user_version`, which leaves a migration path as the database and iOS app evolve.


## Artists
- Kenny Dorham
- Joe Henderson
- Clifford Brown
- Avishai Cohen

python initial_script.py \
  --artist-file artists_to_import.txt \
  --request-budget 250

## To Complete
- Chet Baker
- Miles Davis
- Freddie Hubbard
- Lee Morgan
- Woody Shaw
- Tom Harrell
- Marquis Hill
- Clark Terry
- Steve Fishwick
- Jay Jennings
- Carmell Jones
- Booker Little
- Louis Smith
- Donald Byrd
- Thad Jones
- Dizzy Gillespie
- Dizzy Reece
- Ted Curson
- Roy Hargrove
- Adam O'Farrill
- Dave Douglas
- Yazz Ahmed
- Takuya Kuroda
- Jun Iida
- Matthew Halsall
- Kenny Warren
- Keyon Harrold
- Philip Dizack
- Jack Sheldon
- Howard McGhee
- Joe Gordon
- Joe Wilder
- Don Ellis
- Ingrid Jensen
- Bill Hardman
- Lonnie Hillyer
- Frank Butler
- Rolf Ericson