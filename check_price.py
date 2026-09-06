"""
Bus price-drop checker.

Fetches current bus fares for a route from your busscanner API, tracks
every bus departing at or after a chosen hour (default 9 PM) individually,
and sends a free push notification (via ntfy.sh) to your phone whenever
any one of those buses' fares drops.

Meant to be run on a schedule (see .github/workflows/check-price.yml for a
free GitHub Actions cron setup), but it works fine run by hand or via any
other scheduler (cron, a Raspberry Pi, etc.) too.
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

import requests

# ---- Configuration --------------------------------------------------

API_URL = "https://busscanner-aca.ambitiousbay-f344d961.centralindia.azurecontainerapps.io/api/search/stream"

# Override any of these with environment variables of the same name if you
# want to track a different route/date without editing the script.
SOURCE = os.environ.get("BUS_SOURCE", "Yeswanthpur")
DESTINATION = os.environ.get("BUS_DESTINATION", "Borivali")
DATE = os.environ.get("BUS_DATE", "2026-09-11")

# ntfy.sh is a free, no-signup push notification service. Install the ntfy
# app (Android/iOS) or use https://ntfy.sh in a browser, subscribe to a
# topic name of your choosing, and put that same name here (or set it as
# the NTFY_TOPIC environment variable / GitHub secret).
#
# NOTE: ntfy topics are public by default (anyone who knows the name can
# read/post to it) - pick something long and hard to guess, e.g.
# "aron-bus-alert-8f2ax91q", not just "bus-alert".
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Only consider buses departing at or after this hour (24h clock).
# 21 = 9 PM. Override with the MIN_DEPARTURE_HOUR env var if you want a
# different cutoff later.
MIN_DEPARTURE_HOUR = int(os.environ.get("MIN_DEPARTURE_HOUR", "21"))

# Cap how many individual drops get listed in one push notification, so a
# rare across-the-board price change doesn't produce a giant wall of text.
MAX_DROPS_IN_MESSAGE = 10

STATE_FILE = Path(__file__).parent / "last_prices.json"


# ---- Fetching ---------------------------------------------------------

def fetch_search_results():
    """Fetch bus results from the API.

    Handles both a plain JSON response and a server-sent-events
    ('text/event-stream') response - since the endpoint path ends in
    /stream, it may send incremental provider results before a final
    aggregated payload. Either way, this returns the final parsed dict.
    """
    params = {"source": SOURCE, "destination": DESTINATION, "date": DATE}
    resp = requests.get(API_URL, params=params, stream=True, timeout=60)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        return resp.json()

    last_payload = None
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line or not raw_line.startswith("data:"):
            continue
        chunk = raw_line[len("data:"):].strip()
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "grouped_buses" in payload:
            last_payload = payload  # keep the latest full result seen

    if last_payload is None:
        raise RuntimeError("Could not parse a final result out of the event stream")
    return last_payload


def _departure_hour(time_str):
    """Parse a departure time like '09:30 PM' or '12:00 AM' into its 24h hour."""
    return datetime.strptime(time_str.strip(), "%I:%M %p").hour


def _is_late_enough(group):
    try:
        return _departure_hour(group["departure_time"]) >= MIN_DEPARTURE_HOUR
    except (ValueError, KeyError):
        return False  # skip anything with an unparseable/missing time


def eligible_buses(data):
    """Return every bus group departing at or after MIN_DEPARTURE_HOUR."""
    groups = data.get("grouped_buses") or []
    return [g for g in groups if _is_late_enough(g)]


# ---- Persisted state ----------------------------------------------------

def load_state():
    """Returns {group_id: {"price", "operator", "departure_time", "checked_at"}, ...}"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---- Notifying ------------------------------------------------------------

def notify(title, message):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC is not set - skipping push notification. Message was:")
        print(f"  {title}: {message}")
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "bus,moneybag",
        },
        timeout=15,
    )


# ---- Main -------------------------------------------------------------------

def main():
    data = fetch_search_results()
    buses = eligible_buses(data)
    if not buses:
        raise RuntimeError(
            f"No buses departing at or after {MIN_DEPARTURE_HOUR}:00 were found"
        )

    previous = load_state()
    checked_at = datetime.now(timezone.utc).isoformat()
    new_state = {}
    drops = []

    for bus in buses:
        gid = bus["group_id"]
        price = bus["min_price"]
        operator = bus["operator_name"]
        departure_time = bus["departure_time"]

        new_state[gid] = {
            "price": price,
            "operator": operator,
            "departure_time": departure_time,
            "checked_at": checked_at,
        }

        prev = previous.get(gid)
        if prev is not None and price < prev["price"]:
            drops.append((operator, departure_time, prev["price"], price))

    cheapest = min(buses, key=lambda g: g["min_price"])
    print(
        f"Tracking {len(buses)} buses departing at/after {MIN_DEPARTURE_HOUR}:00. "
        f"Cheapest right now: Rs.{cheapest['min_price']:.0f} "
        f"({cheapest['operator_name']}, departs {cheapest['departure_time']})"
    )

    if not previous:
        notify(
            "Bus price tracking started",
            f"Tracking {len(buses)} buses departing {MIN_DEPARTURE_HOUR}:00+ on "
            f"{SOURCE} -> {DESTINATION}, {DATE}.\n"
            f"Cheapest right now: Rs.{cheapest['min_price']:.0f} "
            f"({cheapest['operator_name']}, departs {cheapest['departure_time']})",
        )
    elif drops:
        lines = [
            f"{operator} ({departure_time}): Rs.{old:.0f} -> Rs.{new:.0f}"
            for operator, departure_time, old, new in drops[:MAX_DROPS_IN_MESSAGE]
        ]
        if len(drops) > MAX_DROPS_IN_MESSAGE:
            lines.append(f"...and {len(drops) - MAX_DROPS_IN_MESSAGE} more")
        notify(
            "Bus price drop!" if len(drops) == 1 else f"{len(drops)} bus prices dropped!",
            f"{SOURCE} -> {DESTINATION} on {DATE} ({MIN_DEPARTURE_HOUR}:00+ departures)\n"
            + "\n".join(lines),
        )
    else:
        print("No price drops since last check.")

    save_state(new_state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)