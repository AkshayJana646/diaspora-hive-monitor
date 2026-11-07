# Diaspora Honey — hive monitoring backend

The server, database and dashboard for the hive monitoring system, with 30
days of realistic generated data so everything can be tested before any
hardware exists.

**The governing rule: the server never calls a hive.** A node sleeps with its
radio off most of the time, so it posts its readings and the server's reply to
that post is the only way anything travels back down. Every design decision
below follows from that.

Hardware is a separate stage and is not in this package.

---

## Run it in two minutes

```bash
pip install -r requirements.txt
python seed_data.py                      # 30 days of data, 3 hives
SESSION_SECRET=dev uvicorn main:app --reload --host 0.0.0.0
```

Open <http://127.0.0.1:8000> and log in as **moe / hivemonitor2026**.

---

## What is here

```
hive_server/
├── main.py             FastAPI server + schema
├── manage.py           add-hive, add-user, list-hives
├── seed_data.py        generates 30 days of history
├── fake_hive.py        posts like a node would, obeys the interval it is given
├── requirements.txt
├── hive.db             pre-seeded, 34,472 readings
└── static/
    ├── index.html      dashboard
    └── login.html
```

---

## The contract

Two messages, and nothing else travels between a node and this server. Freeze
them first: they are what let the hardware and software tracks build in
parallel.

**Up**

```
POST /api/readings
Content-Type: application/json
X-Device-Key: <48 hex characters>

{"site":"temple-hills", "hive_id":"hive-1",
 "interval_min":30, "age_s":0, "seq":4172,
 "readings":{"temp_c":34.8, "humidity_pct":61.2, "weight_kg":41.25,
             "amb_temp_c":8.4, "amb_humidity_pct":71.0,
             "frame1_c":34.3, "frame2_c":33.9, "battery_v":3.98}}
```

**Down**

```
{"interval_min": 30}
```

| Field | Meaning |
| --- | --- |
| `site` | Which apiary. Informational; the database is authoritative |
| `hive_id` | Which hive |
| `interval_min` | What the node is running **right now**. The acknowledgement, not a request |
| `age_s` | Seconds since the reading was taken. 0 for a live reading |
| `seq` | The node's wake counter. Lets a resend be dropped instead of double-counted |
| `readings` | Flat name to number. A new sensor is a new name; the server needs no change |

`401` = wrong key or unknown hive. `422` = bad shape.

**Three rules that prevent most bugs**

- Unit suffix on every metric: `temp_c`, `weight_kg`, `battery_v`
- A failed sensor is **omitted**. Never `0`, never `null` — a weight of 0 kg
  reads as a stolen hive
- The server stamps UTC on arrival. A node has no reliable clock, which is why
  `age_s` exists

---

## Routes

| Route | Caller | Auth | Does |
| --- | --- | --- | --- |
| `POST /api/readings` | node | device key | Stores the readings, replies with the interval |
| `GET /api/hives` | dashboard | login | Every hive, for grouping by site |
| `GET /api/hives/{id}/status` | dashboard | login | last_seen, asked vs reported interval, metric list |
| `GET /api/hives/{id}/latest` | dashboard | login | Most recent value of each metric |
| `GET /api/hives/{id}/readings?metric=&hours=` | dashboard | login | Chart points |
| `POST /api/hives/{id}/settings` | dashboard | login | Stores a new interval, 1 to 1440 |
| `GET/POST /login`, `POST /logout` | browser | — | Session |

---

## The database

Three tables in one SQLite file, created on first run.

```sql
hives(id, name, site, key_hash, interval_min, reported_interval, last_seen)
readings(id, hive_id, ts, metric, value, seq)
users(username, salt, pw_hash)
```

**One row per number.** Eight readings become eight rows, so adding a ninth
sensor needs no migration — the new name simply appears and the dashboard
picks it up.

**`interval_min` vs `reported_interval`** is the whole pending/confirmed
mechanism. The first is what the client asked for; the second is what the node
last reported running. They disagree while a change is in flight, and the
dashboard renders that difference.

**Idempotency.** A unique index on `(hive_id, metric, seq)` drops a reading the
node already delivered. Timestamps cannot do this job: the server stamps
arrival, so a retry gets a later `ts` and would land as a second row, bending
the weight trend.

**Volume.** Eight values every 30 minutes is about 1,150 rows a day per hive,
420,000 a year. Three hives is roughly 50 MB a year. Move to Postgres when the
host wipes its disk on deploy, or when several copies of the server run.

**Back it up.** `sqlite3 hive.db ".backup backup-$(date +%F).db"`, daily.

---

## The generated dataset

`seed_data.py` writes 30 days across three hives at 30-minute intervals —
34,472 readings. Synthetic, but shaped like real hive data, so the dashboard
and any alert logic get exercised against something other than a sine wave.

| Signal | Behaviour |
| --- | --- |
| Ambient | November mid-Atlantic: daily swing on a mean drifting 13 °C down to 7 °C, plus a cold snap and a warm spell |
| Brood nest | Held at 34.5–35.5 °C almost independently of outside |
| Humidity | 50–62% inside, anticorrelated with outside temperature |
| Weight | Daily foraging sawtooth on a slow winter decline, plus rain |
| Frames | Cluster edges run cooler as ambient drops |
| Battery | Charges through daylight, drains overnight |

**Three embedded events**, so the charts have something to show:

| Hive | Event | What it demonstrates |
| --- | --- | --- |
| hive-1 | A six-hour gap five days ago | A dropout, and that gaps render honestly |
| hive-2 | A 9 kg step down on day 18 | A honey super pulled; a step that is **not** a swarm |
| hive-3 | Brood nest drifting 35 °C → 24 °C over ten days | A colony losing thermoregulation while the others hold flat |

Hive-3 is the one that matters. It is the early warning the whole system
exists to produce, visible in the chart days before an inspection would catch
it.

```bash
python seed_data.py --days 7 --step-min 15   # shorter, denser
python seed_data.py --wipe                   # regenerate from scratch
```

---

## Testing

### 1. The charts show the three events

| Where | What you should see |
| --- | --- |
| hive-3 → brood nest temperature → 30 days | Flat at ~35 °C, then a slide to ~24 °C over the last ten days |
| hive-1 → any chart → 7 days | A visible gap about five days ago |
| hive-2 → hive weight → 30 days | A cliff from 50 kg to 41 kg partway through |

### 2. The interval round trip

```bash
python manage.py add-hive hive-9 "Bench node" temple-hills   # prints the key once
python fake_hive.py --hive hive-9 --key <key> --fast         # posts every 5 s
```

Change the dropdown in the dashboard and hit Save. You should see, in order:

```
dashboard  ⏳ Pending — the hive is still on 30 min
terminal   interval 30 -> 15
dashboard  ✓ Confirmed — the hive is using 15 minutes
```

That sequence is the architecture working. If you run one test, run this one.

### 3. Authentication and validation

```bash
KEY=<the key>

# valid → 200 {"interval_min":30}
curl -X POST http://127.0.0.1:8000/api/readings \
  -H "Content-Type: application/json" -H "X-Device-Key: $KEY" \
  -d '{"hive_id":"hive-9","interval_min":30,"age_s":0,"readings":{"temp_c":34.8}}'

curl -i ... -H "X-Device-Key: wrong"          # → 401
curl -i ... -d '{... "readings":{"Temp C":1}}' # → 422, bad metric name
curl -i http://127.0.0.1:8000/api/hives        # → 401, no session
```

### 4. Idempotency

```bash
B='{"hive_id":"hive-9","interval_min":30,"age_s":0,"seq":5001,"readings":{"temp_c":30.0}}'
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -X POST http://127.0.0.1:8000/api/readings \
    -H "Content-Type: application/json" -H "X-Device-Key: $KEY" -d "$B"
done
sqlite3 hive.db "SELECT COUNT(*) FROM readings WHERE hive_id='hive-9' AND seq=5001;"
```

Should print **1**, not 5.

### 5. Query the data directly

```sql
-- hive-3's decline, day by day
SELECT CAST((ts - (strftime('%s','now') - 30*86400))/86400 AS INT) AS day,
       ROUND(AVG(value),2) AS temp_c
FROM readings WHERE hive_id='hive-3' AND metric='temp_c'
GROUP BY day ORDER BY day;

-- how far each nest sits above outside air: the health signal
SELECT a.hive_id, ROUND(AVG(a.value - b.value),1) AS delta_c
FROM readings a JOIN readings b ON a.ts=b.ts AND a.hive_id=b.hive_id
WHERE a.metric='temp_c' AND b.metric='amb_temp_c'
  AND a.ts > strftime('%s','now') - 86400
GROUP BY a.hive_id;
```

That second query is effectively the first alert rule.

### What this does not test

Generated data proves the pipeline and the dashboard. It proves nothing about
sensors, power, calibration or connectivity — those need hardware on a bench.

---

## Adding a site later

The `site` column is already in the schema, which is what makes this a
configuration step rather than a migration:

```bash
python manage.py add-hive hive-7 "Oxon Run A" oxon-run
```

The dashboard groups by site automatically and needs no change. Neither does
any route.

---

## Security

| Lock | Stops | How |
| --- | --- | --- |
| Device key | Fake readings and fake hives | 48 hex characters; the server stores only the SHA-256 hash, compared in constant time |
| Client password | Strangers viewing or changing | Salted PBKDF2, 200,000 rounds, signed session cookie |
| HTTPS | Key or password sniffing | A reverse proxy certificate in production |

**Before the client gets a login**

- [ ] `SESSION_SECRET` is a long random value (`secrets.token_hex(32)`), not in Git
- [ ] `HTTPS_ONLY=1` on the production server
- [ ] Each hive has its own device key
- [ ] The client password is at least 12 characters
- [ ] The database is backed up daily

A device key cannot be recovered, only replaced: delete the hive and run
`add-hive` again.

**Known limitation.** Any logged-in user sees every hive. Fine for one client.
For several, add an `owner` column to `hives` and check it in each dashboard
route.

---

