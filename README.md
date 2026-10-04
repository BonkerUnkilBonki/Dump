# GitHub Releases Dump Bot

Sweeps **every public repo on a GitHub account** and forwards every file
uploaded to their **releases pages** to a Telegram chat — grouped as **one
message per release** (a media album).

- First run **backfills** everything that already exists.
- Then it **keeps watching** (polling) and forwards anything new.
- **De-duplicated by asset id**, so nothing is ever sent twice.

Files: `dump_bot.py`, `requirements.txt`, `env.example`.

---

## What it sends

For each release, all its uploaded assets go out together as one album
(up to 10 files; larger releases are split across a few albums). The caption
carries the repo, tag, release link, and changelog. Assets over 50 MB are
posted as a download link instead of the file (Telegram's bot upload cap).

GitHub's auto-generated "Source code (zip/tar.gz)" entries are **not** real
assets and are never sent — only the files you actually uploaded.

---

## Setup

**1. Create a bot** with [@BotFather](https://t.me/BotFather) → `/newbot`, copy
the token. (You can reuse your existing bot if you like — it just needs to be
an admin in the target channel.)

**2. Add the bot as an admin of `@BonkiDump`** with **Post Messages**
permission.

**3. Get a GitHub token** (strongly recommended). GitHub allows only **60
requests/hour** without one, which a multi-repo sweep will exhaust. Create a
fine-grained token with **Public Repositories → read-only**:
https://github.com/settings/tokens

**4. Configure:**

```bash
cp env.example .env
# edit .env
```

**5. Install and run.**

```bash
pip install -r requirements.txt

# One-time backfill (dumps everything that exists right now):
ONCE=true python dump_bot.py

# Or keep watching (leave it running):
python dump_bot.py
```

`export $(grep -v '^#' .env | xargs)` loads your `.env` into the shell first,
or just set the variables inline.

---

## Deploy on Render (Web Service)

The bot runs fine as a plain **Web Service** — no Blueprint needed. In this
mode it binds Render's `$PORT`, serves a health page at `/`, runs the sweep
loop in the background, and exposes `/sweep` to trigger a sweep on demand.

**R1. Push these files to a GitHub repo** — `dump_bot.py`, `requirements.txt`,
`.gitignore` (and `render.yaml` if you ever want the Blueprint instead). Put
them at the **root** of the repo, not inside a subfolder — Render looks for
`render.yaml` at the root, and the start command assumes the files are there.
Never commit `.env`.

**R2. New + → Web Service** → connect the repo → Runtime **Python**, then set:

| Setting | Value |
| --- | --- |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python dump_bot.py` |
| Health Check Path | `/` |

**R3. Add the environment variables:**

| Key | Value |
| --- | --- |
| `WEB` | `true` |
| `TELEGRAM_BOT_TOKEN` | your BotFather token |
| `GITHUB_TOKEN` | fine-grained PAT, Public Repositories read-only |
| `TELEGRAM_CHANNEL_ID` | `@BonkiDump` |
| `GITHUB_USER` | `BonkerUnkilBonki` |
| `ONCE` | `false` |
| `SWEEP_KEY` | optional random string, protects `/sweep` |

**R4. Deploy.** Open the service URL — you'll get JSON like
`{"status":"ok","watching":"BonkerUnkilBonki",...}`. If that loads, the
service is healthy and Render will mark it Live.

**R5. First run is the backfill.** The loop sweeps every `POLL_INTERVAL`
seconds (default 30 min), so within a minute of deploying you should see
albums arrive in `@BonkiDump`, oldest release first.

### The free-tier catch (and the fix)

Render's **free** web services spin down after ~15 minutes with no incoming
requests, which pauses the sweep loop. Two ways to handle it:

- **Keep it awake with a pinger.** Point a free uptime pinger (cron-job.org,
  UptimeRobot, etc.) at your service URL every 10 minutes. Each hit keeps the
  instance alive; to also trigger a sweep, hit
  `https://<your-service>.onrender.com/sweep?key=<SWEEP_KEY>`.
- **Upgrade the instance** to an always-on paid plan.

### The state-file catch

`dump_state.json` records what has been sent. Free Render instances have an
ephemeral disk, so on a **redeploy** it resets and the bot would re-dump
everything once. To avoid that, attach a **Disk** mounted at `/var/data` and
set `STATE_FILE=/var/data/dump_state.json` (disks need a paid instance). On a
VPS / Pi / your own PC the file simply persists.

---

## Other ways to host the "keep watching" mode

- **Your own PC / a Raspberry Pi / a VPS** — simplest. Run
  `python dump_bot.py` under `systemd`, `screen`/`tmux`, or `pm2`.

To keep it running after closing your terminal:

```bash
nohup python dump_bot.py > dump.log 2>&1 &
```

### Note on the state file

`dump_state.json` records which assets have been sent. On hosts with an
ephemeral disk (like a free Render service) this resets on redeploy — which
would cause a re-dump of everything. On a VPS / Pi / your PC the file persists
normally. If you need durable state on a container host, mount a disk for it.

---

## Options (see env.example)

| Variable | Default | Meaning |
| --- | --- | --- |
| `GITHUB_USER` | — | account to sweep |
| `GITHUB_TOKEN` | — | recommended; lifts the 60/hr limit |
| `TELEGRAM_CHANNEL_ID` | — | where files go (`@BonkiDump`) |
| `ONCE` | `false` | one sweep then exit |
| `WEB` | `false` | run as a web service (bind `$PORT`, serve `/`) |
| `SWEEP_KEY` | — | optional key protecting `/sweep` |
| `POLL_INTERVAL` | `1800` | seconds between sweeps |
| `INCLUDE_PRERELEASES` | `true` | include pre-releases |
| `ASSET_FILTER` | (all) | regex; e.g. `\.apk$` for APKs only |
| `MAX_GROUP` | `10` | files per album |
| `SEND_CHANGELOG` | `true` | changelog in the caption |
| `STATE_FILE` | `dump_state.json` | de-dup memory |

---

## Notes & gotchas

- **One message per release** is a Telegram album (`sendMediaGroup`). A release
  with a single file is sent as a plain document (albums need 2+ items).
- **50 MB cap** per file, as above.
- **Caption limit** is 1024 chars, so long changelogs are trimmed in the album
  caption (the release link is always included, so nothing is lost).
- **Rate limits:** keep `POLL_INTERVAL` sensible (30 min is plenty) and always
  set `GITHUB_TOKEN`.
- **New repos** are picked up automatically on the next sweep — no per-repo
  setup.
