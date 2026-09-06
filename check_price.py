"""
Bus price-drop checker.

Fetches current bus fares for a route from your busscanner API, compares
the cheapest fare to the last time this script ran, and sends a free push
notification (via ntfy.sh) to your phone if the price has dropped.

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

STATE_FILE = Path(__file__).parent / "last_price.json"


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


def cheapest_fare(data):
    """Return (price, operator_name, departure_time) for the cheapest bus
    among those departing at or after MIN_DEPARTURE_HOUR (default 9 PM)."""
    groups = data.get("grouped_buses") or []
    eligible = [g for g in groups if _is_late_enough(g)]
    if not eligible:
        raise RuntimeError(
            f"No buses departing at or after {MIN_DEPARTURE_HOUR}:00 were found"
        )
    best = min(eligible, key=lambda g: g["min_price"])
    return best["min_price"], best["operator_name"], best["departure_time"]


# ---- Persisted state ----------------------------------------------------

def load_last_price():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(price, operator, departure_time):
    STATE_FILE.write_text(json.dumps({
        "price": price,
        "operator": operator,
        "departure_time": departure_time,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


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
    price, operator, departure_time = cheapest_fare(data)
    print(
        f"Cheapest fare departing at/after {MIN_DEPARTURE_HOUR}:00: "
        f"Rs.{price:.0f} ({operator}, departs {departure_time})"
    )

    previous = load_last_price()

    if previous is None:
        notify(
            "Bus price tracking started",
            f"Tracking {SOURCE} -> {DESTINATION} on {DATE}, buses departing "
            f"{MIN_DEPARTURE_HOUR}:00 or later.\n"
            f"Current cheapest fare: Rs.{price:.0f} ({operator}, departs {departure_time})",
        )
    elif price < previous["price"]:
        notify(
            "Bus price dropped!",
            f"{SOURCE} -> {DESTINATION} on {DATE} (departs {MIN_DEPARTURE_HOUR}:00+)\n"
            f"Rs.{previous['price']:.0f} -> Rs.{price:.0f} ({operator}, departs {departure_time})",
        )
    else:
        print("No price drop since last check.")

    save_state(price, operator, departure_time)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)