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

## Deploy on Render (Background Worker)

A Background Worker is the right service type: it runs the polling loop
continuously and needs no public URL. `render.yaml` in this folder sets most
of it up. (Note: background workers are a paid Render service type — the free
plan only covers web services, which spin down after ~15 min idle.)

**R1. Push these files to a GitHub repo** — `dump_bot.py`, `requirements.txt`,
`render.yaml`, and `.gitignore` if you have one. Never commit `.env`.

```bash
git init && git add dump_bot.py requirements.txt render.yaml
git commit -m "github dump bot"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

**R2. Create the worker.** Render Dashboard → **New +** → **Blueprint** →
connect the repo → Render reads `render.yaml` and creates the **Background
Worker**. It prompts you for the two secrets.

(Manual alternative: **New +** → **Background Worker** → connect the repo →
Runtime **Python** → Build `pip install -r requirements.txt` → Start
`python dump_bot.py`. Then add the env vars and the disk by hand.)

**R3. Set the environment variables** (the Blueprint fills most of these; you
supply the two secrets):

| Key | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | your BotFather token |
| `GITHUB_TOKEN` | fine-grained PAT, Public Repositories read-only |
| `TELEGRAM_CHANNEL_ID` | `@BonkiDump` |
| `GITHUB_USER` | `BonkerUnkilBonki` |
| `STATE_FILE` | `/var/data/dump_state.json` |
| `ONCE` | `false` |

**R4. Keep the disk.** `render.yaml` attaches a 1 GB disk mounted at `/var/data`,
and `STATE_FILE` points into it, so the de-dup memory survives restarts and
redeploys. Without this, every restart would re-dump everything.

**R5. Deploy and watch the logs.** On first start the worker does the full
backfill automatically — one album per release, oldest first — then logs
`Sweep complete - 0 asset(s) sent` on the next pass, which confirms de-dup is
working. Check `@BonkiDump` to see the files arrive.

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
