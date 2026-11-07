"""Diaspora Honey hive monitor server.

Run locally:
    SESSION_SECRET=dev uvicorn main:app --reload --host 0.0.0.0

Two messages travel between a hive and this server, and nothing else:

  UP    POST /api/readings  with X-Device-Key
        {"site","hive_id","interval_min","age_s","readings":{name: number}}
  DOWN  {"interval_min": N}

The server never calls a hive. A node is asleep and unreachable most of the
time, so every downlink rides back as the reply to a request the node made.
"""

import hashlib
import hmac
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

DB_PATH = os.environ.get("HIVE_DB", "hive.db")
STATIC = Path(__file__).parent / "static"

# Metric names: lowercase letters, digits and underscore, unit suffix by convention.
METRIC_NAME = re.compile(r"^[a-z0-9_]{1,32}$")

MIN_INTERVAL = 1       # minutes
MAX_INTERVAL = 1440    # 24 hours

app = FastAPI(title="Diaspora Honey hive monitor")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-only-change-me"),
    https_only=os.environ.get("HTTPS_ONLY") == "1",
)


# ---------------------------------------------------------------- database

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS hives(
  id                TEXT PRIMARY KEY,
  name              TEXT,
  site              TEXT NOT NULL DEFAULT 'temple-hills',
  key_hash          TEXT NOT NULL,
  interval_min      INTEGER NOT NULL DEFAULT 30,   -- what the client asked for
  reported_interval INTEGER,                       -- what the hive says it uses
  last_seen         REAL
);

CREATE TABLE IF NOT EXISTS readings(
  id      INTEGER PRIMARY KEY,
  hive_id TEXT NOT NULL REFERENCES hives(id),
  ts      REAL NOT NULL,          -- Unix seconds, UTC, stamped on arrival
  metric  TEXT NOT NULL,          -- e.g. temp_c, weight_kg
  value   REAL NOT NULL,
  seq     INTEGER                 -- the node's wake counter, when it sends one
);
CREATE INDEX IF NOT EXISTS readings_lookup ON readings(hive_id, metric, ts);

-- Idempotency. A node whose POST succeeded but whose reply was lost will
-- resend the same reading on its next wake; without this the duplicate would
-- land twice and bend the weight trend. Timestamps cannot do this job: the
-- server stamps arrival, so a retry gets a different ts. The node's wake
-- counter can, so dedupe on that where it is sent.
CREATE UNIQUE INDEX IF NOT EXISTS readings_unique_seq
  ON readings(hive_id, metric, seq) WHERE seq IS NOT NULL;

CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY,
  salt     BLOB NOT NULL,
  pw_hash  BLOB NOT NULL
);
"""


def init_db():
    with db() as con:
        con.executescript(SCHEMA)


init_db()


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def pw_hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)


# ------------------------------------------------------- 1. the hive route

class Upload(BaseModel):
    hive_id: str
    site: str | None = None            # informational; the DB is authoritative
    interval_min: int | None = None    # what the device is running right now
    age_s: int = Field(0, ge=0, le=7 * 86400)
    seq: int | None = Field(None, ge=0)  # the node's wake counter; dedupes retries
    readings: dict[str, float]


@app.post("/api/readings")
def post_readings(up: Upload, x_device_key: str = Header(...)):
    if not 1 <= len(up.readings) <= 50 or not all(
        METRIC_NAME.match(k) for k in up.readings
    ):
        raise HTTPException(
            422, "readings: 1-50 names, lowercase letters, digits and underscore"
        )

    with db() as con:
        hive = con.execute(
            "SELECT * FROM hives WHERE id = ?", (up.hive_id,)
        ).fetchone()
        if hive is None or not hmac.compare_digest(
            hive["key_hash"], key_hash(x_device_key)
        ):
            raise HTTPException(401, "unknown hive or wrong key")

        now = time.time()
        ts = now - up.age_s
        # OR IGNORE: a resend of a reading this node already delivered hits
        # the unique index on (hive_id, metric, seq) and is dropped.
        con.executemany(
            "INSERT OR IGNORE INTO readings(hive_id, ts, metric, value, seq) "
            "VALUES (?, ?, ?, ?, ?)",
            [(up.hive_id, ts, k, v, up.seq) for k, v in up.readings.items()],
        )
        con.execute(
            "UPDATE hives SET last_seen = ?, reported_interval = ? WHERE id = ?",
            (now, up.interval_min, up.hive_id),
        )

    # The only downlink there is.
    return {"interval_min": hive["interval_min"]}


# -------------------------------------------------- 2. the dashboard routes

def require_user(request: Request):
    if not request.session.get("user"):
        raise HTTPException(401, "log in first")


@app.get("/api/hives")
def list_hives(request: Request):
    """Every hive, grouped by site on the client. Adding a site needs no code."""
    require_user(request)
    with db() as con:
        rows = con.execute(
            "SELECT id, name, site, interval_min, reported_interval, last_seen "
            "FROM hives ORDER BY site, id"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/hives/{hive_id}/status")
def hive_status(hive_id: str, request: Request):
    require_user(request)
    with db() as con:
        h = con.execute(
            "SELECT id, name, site, interval_min, reported_interval, last_seen "
            "FROM hives WHERE id = ?",
            (hive_id,),
        ).fetchone()
        if h is None:
            raise HTTPException(404, "no such hive")
        metrics = [
            r["metric"]
            for r in con.execute(
                "SELECT DISTINCT metric FROM readings WHERE hive_id = ? "
                "ORDER BY metric",
                (hive_id,),
            ).fetchall()
        ]
    out = dict(h)
    out["metrics"] = metrics
    return out


@app.get("/api/hives/{hive_id}/readings")
def hive_readings(
    hive_id: str,
    metric: str,
    request: Request,
    hours: float = Query(48, gt=0, le=24 * 90),
):
    require_user(request)
    if not METRIC_NAME.match(metric):
        raise HTTPException(422, "bad metric name")
    since = time.time() - hours * 3600
    with db() as con:
        rows = con.execute(
            "SELECT ts, value FROM readings "
            "WHERE hive_id = ? AND metric = ? AND ts >= ? ORDER BY ts",
            (hive_id, metric, since),
        ).fetchall()
    return [{"ts": r["ts"], "value": r["value"]} for r in rows]


@app.get("/api/hives/{hive_id}/latest")
def hive_latest(hive_id: str, request: Request):
    """Most recent value of every metric, for the overview cards."""
    require_user(request)
    with db() as con:
        rows = con.execute(
            "SELECT metric, value, ts FROM readings r WHERE hive_id = ? "
            "AND ts = (SELECT MAX(ts) FROM readings WHERE hive_id = r.hive_id "
            "          AND metric = r.metric)",
            (hive_id,),
        ).fetchall()
    return {r["metric"]: {"value": r["value"], "ts": r["ts"]} for r in rows}


# ----------------------------------------------------- 3. the client's knob

class Settings(BaseModel):
    interval_min: int = Field(ge=MIN_INTERVAL, le=MAX_INTERVAL)


@app.post("/api/hives/{hive_id}/settings")
def set_settings(hive_id: str, s: Settings, request: Request):
    require_user(request)
    with db() as con:
        cur = con.execute(
            "UPDATE hives SET interval_min = ? WHERE id = ?",
            (s.interval_min, hive_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "no such hive")
    # Nothing is sent to the hive here. It is asleep. It collects this on its
    # next check-in, which is why the dashboard shows a pending state.
    return {
        "interval_min": s.interval_min,
        "applies": "at the hive's next check-in",
    }


# ---------------------------------------------------------- pages and login

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC / "index.html")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return FileResponse(STATIC / "login.html")


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    with db() as con:
        u = con.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    if u and hmac.compare_digest(u["pw_hash"], pw_hash(password, u["salt"])):
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?error=1", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
