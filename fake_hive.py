"""Stand in for a hive node: post readings and obey the interval the server
sends back. Use this to build and test the dashboard before any hardware runs.

    python fake_hive.py --hive hive-1 --key <device key> --fast

--fast ignores the real interval and posts every 5 seconds, so you can watch
the pending/confirmed round trip in the dashboard without waiting 30 minutes.
Without it, the script sleeps for exactly the interval the server replied
with, which is what a real node does.
"""

import argparse
import math
import random
import time

import requests

DEFAULT_URL = "http://127.0.0.1:8000/api/readings"


def reading_set(t: float) -> dict:
    """One plausible sample. Shape matters more than the exact numbers here."""
    hour = (t % 86400) / 3600.0
    amb = 9.0 + 6.0 * math.sin((hour - 9.5) / 24.0 * 2 * math.pi) + random.gauss(0, 0.4)
    brood = 34.9 + random.gauss(0, 0.2)
    return {
        "temp_c": round(brood, 2),
        "humidity_pct": round(57.0 + random.gauss(0, 1.8), 1),
        "weight_kg": round(42.0 + random.gauss(0, 0.05), 3),
        "amb_temp_c": round(amb, 2),
        "amb_humidity_pct": round(max(35.0, 88.0 - 2.6 * amb + random.gauss(0, 3)), 1),
        "frame1_c": round(brood - 0.6 + random.gauss(0, 0.2), 2),
        "frame2_c": round(brood - 1.1 + random.gauss(0, 0.25), 2),
        "battery_v": round(3.95 + random.gauss(0, 0.02), 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hive", default="hive-1")
    ap.add_argument("--site", default="temple-hills")
    ap.add_argument("--key", required=True, help="device key from manage.py")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--fast", action="store_true",
                    help="post every 5 s regardless of the interval")
    args = ap.parse_args()

    interval_min = 30          # what this "device" believes it is running

    while True:
        body = {
            "site": args.site,
            "hive_id": args.hive,
            "interval_min": interval_min,   # the acknowledgement
            "age_s": 0,
            "readings": reading_set(time.time()),
        }
        try:
            r = requests.post(
                args.url, json=body,
                headers={"X-Device-Key": args.key}, timeout=10,
            )
            if r.status_code == 200:
                new = r.json().get("interval_min")
                if isinstance(new, int) and 1 <= new <= 1440 and new != interval_min:
                    print(f"  interval {interval_min} -> {new} (saved, persists across restarts)")
                    interval_min = new
                print(f"{time.strftime('%H:%M:%S')}  200  using {interval_min} min")
            else:
                print(f"{time.strftime('%H:%M:%S')}  {r.status_code}  {r.text[:80]}")
        except Exception as e:                      # keep the reading, retry
            print(f"{time.strftime('%H:%M:%S')}  send failed: {e}")

        time.sleep(5 if args.fast else interval_min * 60)


if __name__ == "__main__":
    main()
