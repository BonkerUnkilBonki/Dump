#!/usr/bin/env python3
"""
GitHub Releases Dump Bot.

Sweeps EVERY public repo on a GitHub account and forwards every release
asset (the files uploaded to the releases page) to a Telegram chat, grouped
as one message per release (a media album).

  * First run backfills everything that already exists.
  * Then it keeps watching (polling) and forwards anything new.
  * De-duplicated by asset id, so nothing is ever sent twice.

Run once:      ONCE=true python dump_bot.py
Run forever:   python dump_bot.py            (on an always-on host)

All configuration is via environment variables - see env.example / README.md.
"""

import json
import logging
import os
import re
import sys
import tempfile
import threading
import time

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()

GITHUB_USER = os.environ.get("GITHUB_USER", "").strip()          # account to sweep
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()        # recommended

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "1800"))     # seconds between sweeps
ONCE = os.environ.get("ONCE", "false").lower() == "true"         # single sweep then exit

INCLUDE_PRERELEASES = os.environ.get("INCLUDE_PRERELEASES", "true").lower() != "false"
ASSET_FILTER = os.environ.get("ASSET_FILTER", "").strip()        # regex; default = all assets
MAX_GROUP = int(os.environ.get("MAX_GROUP", "10"))               # Telegram album limit
SEND_CHANGELOG = os.environ.get("SEND_CHANGELOG", "true").lower() != "false"

STATE_FILE = os.environ.get("STATE_FILE", "dump_state.json")
SEEN_TTL = int(os.environ.get("SEEN_TTL", str(30 * 24 * 3600)))  # forget asset ids after 30 days

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TG_CAPTION_LIMIT = 1024
TG_MESSAGE_LIMIT = 4096
TG_UPLOAD_LIMIT = 49 * 1024 * 1024   # Bot API caps document uploads at 50 MB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dump-bot")


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def _tg(method, data=None, files=None, timeout=600):
    resp = requests.post(f"{TG_API}/{method}", data=data, files=files, timeout=timeout)
    try:
        payload = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"Non-JSON reply from Telegram: {resp.text[:200]}")
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description')}")
    return payload.get("result")


def _chunks(text, limit):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


def send_message(text):
    for piece in _chunks(text, TG_MESSAGE_LIMIT):
        if piece.strip():
            _tg("sendMessage", data={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "text": piece,
                "disable_web_page_preview": "true",
            })


def send_document(path, filename, caption=""):
    if len(caption) > TG_CAPTION_LIMIT:
        caption = caption[: TG_CAPTION_LIMIT - 1] + "\u2026"
    with open(path, "rb") as fh:
        return _tg("sendDocument", data={
            "chat_id": TELEGRAM_CHANNEL_ID,
            "caption": caption,
        }, files={"document": (filename, fh)})


def send_media_group(files, caption=""):
    """Send 2..10 documents as one album. `files` is a list of (path, name)."""
    if len(caption) > TG_CAPTION_LIMIT:
        caption = caption[: TG_CAPTION_LIMIT - 1] + "\u2026"
    media, handles, opened = [], {}, []
    try:
        for i, (path, name) in enumerate(files):
            key = f"f{i}"
            fh = open(path, "rb")
            opened.append(fh)
            handles[key] = (name, fh)
            item = {"type": "document", "media": f"attach://{key}"}
            if i == 0:
                item["caption"] = caption
            media.append(item)
        return _tg("sendMediaGroup", data={
            "chat_id": TELEGRAM_CHANNEL_ID,
            "media": json.dumps(media),
        }, files=handles)
    finally:
        for fh in opened:
            fh.close()


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

def _gh_headers(accept="application/vnd.github+json"):
    headers = {"Accept": accept}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def gh_get(url, params=None):
    while True:
        resp = requests.get(url, headers=_gh_headers(), params=params, timeout=30)
        if resp.status_code in (403, 429):
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset = resp.headers.get("X-RateLimit-Reset")
            if remaining == "0" and reset:
                wait = max(1, int(reset) - int(time.time()) + 2)
                log.warning("GitHub rate limit hit - sleeping %ds", wait)
                time.sleep(min(wait, 3600))
                continue
            log.warning("GitHub %s returned %s", url, resp.status_code)
        resp.raise_for_status()
        return resp.json()


def _paginate(url, params=None):
    params = dict(params or {})
    params.setdefault("per_page", "100")
    page = 1
    while True:
        params["page"] = str(page)
        batch = gh_get(url, params=params)
        if not batch:
            return
        for item in batch:
            yield item
        if len(batch) < int(params["per_page"]):
            return
        page += 1


def list_public_repos(user):
    return [r for r in _paginate(f"https://api.github.com/users/{user}/repos",
                                 {"type": "public"})
            if not r.get("private")]


def list_releases(full_name):
    return list(_paginate(f"https://api.github.com/repos/{full_name}/releases"))


def download_asset(asset, dest_path):
    headers = {"Accept": "application/octet-stream"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    with requests.get(asset["url"], headers=headers, stream=True,
                      allow_redirects=True, timeout=300) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as fh:
            for block in resp.iter_content(chunk_size=256 * 1024):
                fh.write(block)
    return dest_path


def match_asset(name):
    if not ASSET_FILTER:
        return True
    return bool(re.search(ASSET_FILTER, name or ""))


# --------------------------------------------------------------------------
# Persistent state (which assets have already been sent)
# --------------------------------------------------------------------------

_state_lock = threading.Lock()


def _load_state():
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        log.warning("Could not persist state: %s", exc)


def _prune(sent):
    now = time.time()
    for key in [k for k, v in sent.items() if now - v > SEEN_TTL]:
        sent.pop(key, None)


def is_sent(asset_id):
    with _state_lock:
        return str(asset_id) in _load_state().get("sent", {})


def mark_sent(asset_id):
    with _state_lock:
        state = _load_state()
        sent = state.setdefault("sent", {})
        sent[str(asset_id)] = time.time()
        _prune(sent)
        _save_state(state)


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

def _caption(full_name, rel):
    title = rel.get("name") or rel.get("tag_name") or "release"
    tag = rel.get("tag_name", "")
    url = rel.get("html_url", "")
    body = (rel.get("body") or "").strip()
    cap = f"\U0001F4E6 {title} ({tag})\n{full_name}\n{url}"
    if SEND_CHANGELOG and body:
        cap += f"\n\n{body}"
    return cap


def process_release(full_name, rel):
    """Send any not-yet-sent assets of one release, grouped in albums."""
    assets = [a for a in rel.get("assets", []) if match_asset(a.get("name"))]
    pending = [a for a in assets if not is_sent(a["id"])]
    if not pending:
        return 0

    tag = rel.get("tag_name", "?")
    log.info("%s %s: %d asset(s) to send", full_name, tag, len(pending))
    caption = _caption(full_name, rel)
    sent_count = 0

    # Split into files we can upload vs. ones too big for the Bot API.
    small, big = [], []
    for a in pending:
        (small if a.get("size", 0) <= TG_UPLOAD_LIMIT else big).append(a)

    # big files: post a link instead of the file
    for a in big:
        mb = a.get("size", 0) / (1024 * 1024)
        send_message(f"{caption}\n\n\u26A0 {a['name']} is {mb:.1f} MB, over "
                     f"Telegram's 50 MB limit.\nDownload: {a.get('browser_download_url','')}")
        mark_sent(a["id"])
        sent_count += 1

    if not small:
        return sent_count

    # download into one temp dir, then send in groups of MAX_GROUP
    with tempfile.TemporaryDirectory() as tmp:
        local = []
        for a in small:
            dest = os.path.join(tmp, f"{a['id']}_{a['name']}")
            try:
                log.info("Downloading %s (%.1f MB)", a["name"], a.get("size", 0) / (1024 * 1024))
                download_asset(a, dest)
            except Exception as exc:                    # noqa: BLE001
                log.warning("Download of %s failed: %s", a["name"], exc)
                continue
            local.append((a, dest))

        for start in range(0, len(local), MAX_GROUP):
            batch = local[start:start + MAX_GROUP]
            cap = caption if start == 0 else f"\u2026 (continued) {tag} - {full_name}"
            try:
                if len(batch) == 1:
                    send_document(batch[0][1], batch[0][0]["name"], cap)
                else:
                    send_media_group([(d, a["name"]) for a, d in batch], cap)
            except Exception as exc:                    # noqa: BLE001
                log.warning("Upload failed for %s: %s", tag, exc)
                continue
            for a, _ in batch:
                mark_sent(a["id"])
                sent_count += 1
    return sent_count


def sweep():
    """One full pass over the account. Returns number of assets sent."""
    repos = list_public_repos(GITHUB_USER)
    log.info("Found %d public repo(s) for %s", len(repos), GITHUB_USER)

    work = []
    for repo in repos:
        full = repo["full_name"]
        try:
            releases = list_releases(full)
        except Exception as exc:                        # noqa: BLE001
            log.warning("Could not list releases for %s: %s", full, exc)
            continue
        for rel in releases:
            if rel.get("draft"):
                continue
            if rel.get("prerelease") and not INCLUDE_PRERELEASES:
                continue
            if not rel.get("assets"):
                continue
            work.append((full, rel))

    # oldest first, so the channel reads chronologically
    work.sort(key=lambda x: x[1].get("published_at") or x[1].get("created_at") or "")

    total = 0
    for full, rel in work:
        try:
            total += process_release(full, rel)
        except Exception as exc:                        # noqa: BLE001
            log.warning("Failed on %s %s: %s", full, rel.get("tag_name"), exc)
    log.info("Sweep complete - %d asset(s) sent", total)
    return total


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID (see README.md).")
    if not GITHUB_USER:
        sys.exit("Set GITHUB_USER to the account whose public repos to sweep.")
    if not GITHUB_TOKEN:
        log.warning("GITHUB_TOKEN is empty - unauthenticated GitHub allows only "
                    "60 requests/hour, which may be too few for a full sweep.")

    if ONCE:
        sweep()
        return

    log.info("Watching %s every %ds (Ctrl-C to stop)", GITHUB_USER, POLL_INTERVAL)
    while True:
        try:
            sweep()
        except Exception as exc:                        # noqa: BLE001
            log.warning("Sweep error: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
