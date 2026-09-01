# Deploying the Keepa scan to Hetzner

Follows the same conventions as `hyperaster` on the same box: code in
`/opt/keepa`, venv at `/opt/keepa/.venv`, secrets in `/opt/keepa/.env` (mode
600, never committed), unit files here and copied to `/etc/systemd/system/`.

## Why a timer at all

A scan is a ~2-hour job that spends most of its wall clock BLOCKED on token
refill (300-token bucket, 5/min). That is not something to run on a laptop that
sleeps, and not something worth babysitting. It is the whole reason this
service exists.

## Install

```bash
ssh root@<host>
git clone https://github.com/lukasbecker36-dot/Keepa.git /opt/keepa
cd /opt/keepa
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
printf 'KEEPA_API_KEY=<key>\n' > .env && chmod 600 .env
.venv/bin/python -m pytest tests/ -q          # no network needed

cp deploy/keepa-scan.service deploy/keepa-scan.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now keepa-scan.timer
```

## Operate

```bash
systemctl list-timers keepa-scan.timer     # when it next fires
systemctl start keepa-scan.service         # run once, now
journalctl -u keepa-scan -f                # watch it
journalctl -u keepa-scan --since yesterday # last night's run
```

Output lands in `/opt/keepa/output/YYYY-MM-DD/`. Fetch it with:

```bash
scp -r root@<host>:/opt/keepa/output/$(date +%F) ./
```

## Sharing the box with hyperaster

The unit is deliberately deferential: `Nice=10`, `CPUWeight=20`, `IOWeight=20`
and `MemoryMax=1G`. The scan is I/O-bound and mostly asleep, so it should never
compete with the trading services — but if it ever misbehaves, systemd caps it
rather than the trader losing out.

## Re-scoring costs nothing

Thresholds are tuned against the cache, not the API:

```bash
/opt/keepa/.venv/bin/python run.py --rescore
```

At 5 tokens/min a refetch is an hour; re-scoring stored JSON is instant and
free. `data/keepa.db` is the accumulated history and is worth keeping.
