"""Generate a realistic 30-day history for three hives at Temple Hills.

    python seed_data.py            # 30 days, 30-minute interval
    python seed_data.py --days 7

Writes straight into hive.db, creating the hives and a client login if they
are not there yet. Device keys are printed so a real node or fake_hive.py can
post against the same hives.

The data is synthetic but shaped like real hive data, so the dashboard and any
alert logic get exercised against something other than a sine wave:

  ambient      November mid-Atlantic: a daily swing around a mean that drifts
               down over the month, plus a cold snap and a warm spell
  brood nest   bees hold 34.5-35.5 C almost independently of outside
  humidity     50-62% inside, anticorrelated with outside temperature
  weight       a daily foraging sawtooth on a slow winter decline, plus rain
  frames       cluster edges run cooler than the centre as ambient drops
  battery      charges through daylight, drains overnight

Three embedded events, so the charts have something to show:

  hive-1   a six-hour gap 5 days ago (Wi-Fi dropout)
  hive-2   a 9 kg step down on day 18 (Moe pulled a honey super)
  hive-3   a colony losing thermoregulation over the final 10 days
"""

import argparse
import math
import os
import random
import secrets
import sys
import time

os.environ.setdefault("SESSION_SECRET", "not-used-here")

from main import db, key_hash, pw_hash  # noqa: E402

DAY = 86400.0
random.seed(20261107)  # deterministic: re-running gives the same dataset

HIVES = [
    # id,       name,                 site,           base_kg, story
    ("hive-1", "Temple Hills A",      "temple-hills", 42.6, "healthy"),
    ("hive-2", "Temple Hills B",      "temple-hills", 51.2, "super-pulled"),
    ("hive-3", "Temple Hills C",      "temple-hills", 38.4, "declining"),
]


def ambient_temp(t_days: float) -> float:
    """Celsius. Daily swing on a mean that cools through November."""
    seasonal = 13.0 - 0.21 * t_days                     # 13 C down to ~7 C
    hour = (t_days % 1.0) * 24.0
    diurnal = 6.2 * math.sin((hour - 9.5) / 24.0 * 2 * math.pi)
    # A cold snap around day 12 and a warm spell around day 23.
    weather = -5.0 * math.exp(-((t_days - 12.0) ** 2) / 3.0)
    weather += 4.0 * math.exp(-((t_days - 23.0) ** 2) / 4.0)
    return seasonal + diurnal + weather + random.gauss(0, 0.45)


def ambient_rh(amb_c: float) -> float:
    """Outside humidity: high when cold, lower in the warm part of the day."""
    rh = 88.0 - 2.6 * amb_c + random.gauss(0, 3.5)
    return max(32.0, min(99.0, rh))


def is_rain(t_days: float) -> bool:
    """Two rain events in the month."""
    return (7.3 < t_days < 7.9) or (19.1 < t_days < 20.0)


def daylight(t_days: float) -> float:
    """0 at night, up to 1 at midday. November daylight, roughly 07:00-17:00."""
    hour = (t_days % 1.0) * 24.0
    if hour < 7.0 or hour > 17.0:
        return 0.0
    return math.sin((hour - 7.0) / 10.0 * math.pi)


def forage_activity(t_days: float, amb_c: float) -> float:
    """0 to 1. Bees fly above about 12 C, in daylight, not in rain."""
    if is_rain(t_days) or amb_c < 11.0:
        return 0.0
    return daylight(t_days) * min(1.0, (amb_c - 11.0) / 6.0)


def brood_temp(story: str, t_days: float, days: float, amb_c: float) -> float:
    """Healthy colonies hold 34.5-35.5 C. A failing one drifts toward ambient."""
    setpoint = 34.9 + 0.22 * math.sin(t_days * 2 * math.pi / 3.1)
    if story == "declining":
        # Over the final 10 days, regulation decays: the nest tracks outside air.
        # Capped: this is a colony in trouble that a beekeeper could still
        # save, not one already lost. The point is the early warning.
        decay = max(0.0, (t_days - (days - 10.0)) / 10.0)
        coupling = min(0.40, 0.40 * decay ** 1.5)
        noise = random.gauss(0, 0.18 + 1.1 * coupling)
        return setpoint * (1 - coupling) + amb_c * coupling + noise
    return setpoint + random.gauss(0, 0.19)


def build_rows(hive_id, story, base_kg, days, step_min, now):
    """Return [(ts, metric, value), ...] for one hive."""
    rows = []
    steps = int(days * 24 * 60 / step_min)
    weight = base_kg
    battery = 4.02

    for i in range(steps):
        t_days = i * step_min / 1440.0            # 0 .. days
        ts = now - (days - t_days) * DAY

        # --- hive-1 loses connectivity for six hours, five days ago ---
        if story == "healthy" and (days - 5.0) < t_days < (days - 4.75):
            continue

        amb_c = ambient_temp(t_days)
        amb_h = ambient_rh(amb_c)
        activity = forage_activity(t_days, amb_c)

        # --- weight -------------------------------------------------------
        # Slow winter decline: the colony eats its stores.
        decline = 0.055 if story != "declining" else 0.085   # kg per day
        weight = base_kg - decline * t_days
        # Daily sawtooth: foragers away by day, home with nectar by evening.
        weight -= 0.42 * activity
        if activity > 0.05:
            weight += 0.30 * activity * max(0.0, (amb_c - 12.0) / 8.0)
        if is_rain(t_days):
            weight += 0.28                      # water on the hive and stand
        # Moe pulls a honey super from hive-2 on day 18.
        if story == "super-pulled" and t_days > 18.0:
            weight -= 9.1
        weight += random.gauss(0, 0.035)        # load-cell noise after median

        # --- temperatures -------------------------------------------------
        t_in = brood_temp(story, t_days, days, amb_c)
        # Cluster edges sit cooler than the centre, more so when it is cold.
        edge_drop = 0.5 + max(0.0, (14.0 - amb_c)) * 0.085
        if story == "declining":
            edge_drop *= 1.0 + 1.9 * max(0.0, (t_days - (days - 10.0)) / 10.0)
        f1 = t_in - edge_drop * 0.55 + random.gauss(0, 0.22)
        f2 = t_in - edge_drop * 0.95 + random.gauss(0, 0.26)

        # --- humidity -----------------------------------------------------
        rh_in = 57.0 + 0.11 * (70.0 - amb_h) * -1 + random.gauss(0, 1.6)
        if is_rain(t_days):
            rh_in += 5.5
        if story == "declining":
            # Less fanning, so moisture builds up.
            rh_in += 9.0 * max(0.0, (t_days - (days - 10.0)) / 10.0)
        rh_in = max(38.0, min(92.0, rh_in))

        # --- battery ------------------------------------------------------
        sun = daylight(t_days) * (0.25 if is_rain(t_days) else 1.0)
        battery += 0.028 * sun - 0.0065
        battery = max(3.68, min(4.16, battery + random.gauss(0, 0.004)))

        rows += [
            (ts, "temp_c", round(t_in, 2)),
            (ts, "humidity_pct", round(rh_in, 1)),
            (ts, "weight_kg", round(weight, 3)),
            (ts, "amb_temp_c", round(amb_c, 2)),
            (ts, "amb_humidity_pct", round(amb_h, 1)),
            (ts, "frame1_c", round(f1, 2)),
            (ts, "frame2_c", round(f2, 2)),
            (ts, "battery_v", round(battery, 3)),
        ]
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--step-min", type=int, default=30)
    ap.add_argument("--wipe", action="store_true",
                    help="delete existing readings for these hives first")
    args = ap.parse_args()

    now = time.time()
    keys = {}

    with db() as con:
        for hive_id, name, site, _base, _story in HIVES:
            existing = con.execute(
                "SELECT id FROM hives WHERE id = ?", (hive_id,)
            ).fetchone()
            if existing is None:
                key = secrets.token_hex(24)
                keys[hive_id] = key
                con.execute(
                    "INSERT INTO hives(id, name, site, key_hash, interval_min, "
                    "reported_interval, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (hive_id, name, site, key_hash(key),
                     args.step_min, args.step_min, now),
                )
            else:
                con.execute(
                    "UPDATE hives SET last_seen = ?, reported_interval = ? "
                    "WHERE id = ?",
                    (now, args.step_min, hive_id),
                )

        if args.wipe:
            con.execute(
                "DELETE FROM readings WHERE hive_id IN (?, ?, ?)",
                tuple(h[0] for h in HIVES),
            )

        total = 0
        for hive_id, _name, _site, base, story in HIVES:
            rows = build_rows(hive_id, story, base, args.days,
                              args.step_min, now)
            con.executemany(
                "INSERT OR IGNORE INTO readings(hive_id, ts, metric, value) "
                "VALUES (?, ?, ?, ?)",
                [(hive_id, ts, m, v) for ts, m, v in rows],
            )
            total += len(rows)
            print(f"  {hive_id}: {len(rows):>6,} readings  ({story})")

        # A client login, if there is none yet.
        if con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
            salt = secrets.token_bytes(16)
            con.execute(
                "INSERT INTO users(username, salt, pw_hash) VALUES (?, ?, ?)",
                ("moe", salt, pw_hash("hivemonitor2026", salt)),
            )
            print("\n  Login created:  moe / hivemonitor2026")

    print(f"\n  {total:,} rows written over {args.days:g} days "
          f"at {args.step_min}-minute intervals.")
    if keys:
        print("\n  Device keys (shown once; one per node):")
        for hid, k in keys.items():
            print(f"    {hid}  {k}")


if __name__ == "__main__":
    sys.exit(main())
