#!/usr/bin/env python3
"""Export a boat's complete NoForeignLand journey to a structured GPX file.

Fetches all known fixes for a boat from NoForeignLand's public journey API,
then organizes them into a GPX file where:

  - each NoForeignLand "chapter" (a named leg of the journey, e.g. "Refit in
    Almerimar") becomes its own GPX <trk>
  - within a chapter, points are further split into separate <trkseg>
    segments whenever the gap between two consecutive fixes exceeds
    --gap-hours, so that distinct continuous trips (as opposed to the boat
    sitting still between voyages) don't get drawn as one connected line

Usage:
    python3 noforeignland_journey_gpx.py --boat-id 5966441343877120 --output tinarasia.gpx
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request
from xml.etree import ElementTree as ET
from xml.dom import minidom

API_BASE = "https://www.noforeignland.com/api/v1"
USER_AGENT = "Mozilla/5.0 (compatible; noforeignland-journey-gpx/1.0)"

# NoForeignLand fixes use millisecond epoch timestamps. Anything older than
# roughly the year 2000 is bogus placeholder data (e.g. a story with no
# actual fix time attached) and gets dropped.
MIN_SANE_TIME_MS = 946_684_800_000


def fetch_journey(boat_id: int, base_url: str = API_BASE) -> dict:
    """Fetch the full journey payload (fixes + chapters) for a boat."""
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    params = {
        "boatId": boat_id,
        "startDate": 0,
        "endDate": now_ms,
        "showStories": "true",
    }
    url = f"{base_url}/boat/journey?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def extract_points(journey: dict) -> list[tuple[int, float, float]]:
    """Flatten the journey's GeoJSON features into (time_ms, lat, lon) points."""
    points: list[tuple[int, float, float]] = []

    for feature in journey.get("geojson", {}).get("features", []):
        geometry = feature.get("geometry", {})
        geom_type = geometry.get("type")
        coordinates = geometry.get("coordinates", [])

        if geom_type == "Point":
            lon, lat, t = coordinates
            candidates = [(t, lat, lon)]
        elif geom_type == "LineString":
            candidates = [(t, lat, lon) for lon, lat, t in coordinates]
        else:
            continue

        for t, lat, lon in candidates:
            if t and t > MIN_SANE_TIME_MS:
                points.append((int(t), lat, lon))

    points.sort(key=lambda p: p[0])
    return points


def assign_chapters(
    journey: dict, points: list[tuple[int, float, float]]
) -> list[tuple[str, list[tuple[int, float, float]]]]:
    """Group points into (chapter_name, points) buckets, in chronological order.

    Chapter boundaries come from NoForeignLand's own "chapters" list (each
    with a start time; the boundary to the next chapter is used as its end,
    so there are no gaps or overlaps). Points before the first chapter's
    start, or when no chapters exist at all, are bucketed under
    "Unassigned".
    """
    chapters = sorted(journey.get("chapters") or [], key=lambda c: c["start"])

    if not chapters:
        return [("Unassigned", points)] if points else []

    boundaries: list[tuple[str, int, float]] = []
    for i, chapter in enumerate(chapters):
        start = chapter["start"]
        end = chapters[i + 1]["start"] if i + 1 < len(chapters) else float("inf")
        boundaries.append((chapter["name"], start, end))

    buckets: list[tuple[str, list[tuple[int, float, float]]]] = [
        ("Unassigned", [])
    ]
    buckets += [(name, []) for name, _, _ in boundaries]

    for point in points:
        t = point[0]
        placed = False
        for i, (name, start, end) in enumerate(boundaries):
            if start <= t < end:
                buckets[i + 1][1].append(point)
                placed = True
                break
        if not placed:
            buckets[0][1].append(point)

    # Drop empty buckets (e.g. no leftover points before the first chapter).
    return [(name, pts) for name, pts in buckets if pts]


def split_into_trips(
    points: list[tuple[int, float, float]], gap_hours: float
) -> list[list[tuple[int, float, float]]]:
    """Split a chronological list of points into segments (continuous trips).

    A new segment starts whenever the time gap between two consecutive
    points exceeds gap_hours, on the assumption that a long pause means the
    boat was moored/anchored rather than actively underway.
    """
    if not points:
        return []

    gap_ms = gap_hours * 60 * 60 * 1000
    trips: list[list[tuple[int, float, float]]] = [[points[0]]]

    for prev, current in zip(points, points[1:]):
        if current[0] - prev[0] > gap_ms:
            trips.append([])
        trips[-1].append(current)

    return trips


def build_gpx(
    chaptered_points: list[tuple[str, list[tuple[int, float, float]]]],
    gap_hours: float,
    boat_name: str,
) -> ET.Element:
    gpx = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": "noforeignland_journey_gpx.py",
            "xmlns": "http://www.topografix.com/GPX/1/1",
        },
    )

    for chapter_name, points in chaptered_points:
        trips = split_into_trips(points, gap_hours)

        trk = ET.SubElement(gpx, "trk")
        ET.SubElement(trk, "name").text = f"{boat_name} – {chapter_name}"

        for trip in trips:
            trkseg = ET.SubElement(trk, "trkseg")
            for t, lat, lon in trip:
                trkpt = ET.SubElement(
                    trkseg, "trkpt", {"lat": f"{lat}", "lon": f"{lon}"}
                )
                iso_time = dt.datetime.fromtimestamp(
                    t / 1000, tz=dt.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                ET.SubElement(trkpt, "time").text = iso_time

    return gpx


def write_gpx(gpx: ET.Element, output_path: str) -> None:
    rough_xml = ET.tostring(gpx, encoding="unicode")
    pretty_xml = minidom.parseString(rough_xml).toprettyxml(indent="  ")
    # minidom adds its own XML declaration; keep just one.
    pretty_xml = "\n".join(
        line for line in pretty_xml.splitlines() if line.strip()
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(pretty_xml + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--boat-id",
        type=int,
        required=True,
        help="NoForeignLand boat ID (found in the embed/share URL for the boat's map)",
    )
    parser.add_argument(
        "--boat-name",
        default="Boat",
        help="Name to use in GPX track names (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        default="journey.gpx",
        help="Path to write the GPX file to (default: %(default)s)",
    )
    parser.add_argument(
        "--gap-hours",
        type=float,
        default=12.0,
        help=(
            "Minimum time gap, in hours, between fixes before starting a new "
            "trip segment within a chapter (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=API_BASE,
        help="NoForeignLand API base URL (default: %(default)s)",
    )
    args = parser.parse_args()

    print(f"Fetching journey for boat {args.boat_id}...", file=sys.stderr)
    journey = fetch_journey(args.boat_id, args.base_url)

    points = extract_points(journey)
    print(f"Got {len(points)} dated fixes.", file=sys.stderr)

    chaptered_points = assign_chapters(journey, points)
    for name, pts in chaptered_points:
        trips = split_into_trips(pts, args.gap_hours)
        print(f"  {name}: {len(pts)} fixes across {len(trips)} trip(s)", file=sys.stderr)

    gpx = build_gpx(chaptered_points, args.gap_hours, args.boat_name)
    write_gpx(gpx, args.output)
    print(f"Wrote {args.output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
