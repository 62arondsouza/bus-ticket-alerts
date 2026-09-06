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


def cheapest_fare(data):
    """Return (price, operator_name) for the single cheapest bus found."""
    groups = data.get("grouped_buses") or []
    if not groups:
        raise RuntimeError("No buses found in the response")
    best = min(groups, key=lambda g: g["min_price"])
    return best["min_price"], best["operator_name"]


# ---- Persisted state ----------------------------------------------------

def load_last_price():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(price, operator):
    STATE_FILE.write_text(json.dumps({
        "price": price,
        "operator": operator,
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
    price, operator = cheapest_fare(data)
    print(f"Current cheapest fare: Rs.{price:.0f} ({operator})")

    previous = load_last_price()

    if previous is None:
        notify(
            "Bus price tracking started",
            f"Tracking {SOURCE} -> {DESTINATION} on {DATE}.\n"
            f"Current cheapest fare: Rs.{price:.0f} ({operator})",
        )
    elif price < previous["price"]:
        notify(
            "Bus price dropped!",
            f"{SOURCE} -> {DESTINATION} on {DATE}\n"
            f"Rs.{previous['price']:.0f} -> Rs.{price:.0f} ({operator})",
        )
    else:
        print("No price drop since last check.")

    save_state(price, operator)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)