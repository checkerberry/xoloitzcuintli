#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2025 Mjau
# SPDX-License-Identifier: MPL-2.0
"""TRTHNBB NationStates API Recruitment Telegram Bot.

Typical usage example:

  export NS_REGION="The Region That Has No Big Banks"
  export NS_USER_AGENT="xoloitzcuintli/1.0 (contact: you@example.com)"
  export NS_TG_CLIENT="YOUR-TELEGRAM-API-CLIENT-KEY"
  export NS_TG_ID="12345678"           # TGID shown on the telegram page
  export NS_TG_SECRET="abcdef123456"   # Secret Key shown on the telegram page

  python3 xoloitzcuintli.py \
      --storage db.sqlite3 \
      --max-candidates 50 \
      --dry-run=false \
      --loop

Notes
-----
* Recruitment TG rate limit (at time of writing): 1 / 180 seconds,
  enforced per API Client Key (region-wide). The bot abides by the
  `X-Retry-After` header from the Telegrams API and the standard
  API-wide `Retry-After`/`RateLimit-*` headers.
* To legally send TGs via scripts, you must:
    1) Obtain a Telegrams API Client Key from moderators.
    2) Compose your telegram to `tag:api` in-game and note its TGID
       and Secret Key.
    3) Supply those values via env vars or CLI flags.
* To be gentle on the API, target discovery combines shards where
  useful and uses transparent backoff based on server headers.
* For more API info, see: https://www.nationstates.net/pages/api.html
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import requests


_NS_API = "https://www.nationstates.net/cgi-bin/api.cgi"
_SEND_TG = "https://www.nationstates.net/cgi-bin/api.cgi"
_DB_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sent_log (
  nation TEXT PRIMARY KEY,
  sent_ts INTEGER NOT NULL,
  status TEXT NOT NULL,
  note   TEXT
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


@dataclass(frozen=True)
class Config:
    """Runtime configuration for the recruiter."""

    region: str
    user_agent: str
    tg_client: str
    tg_id: str
    tg_secret: str
    storage_path: str
    max_candidates: int
    dry_run: bool
    loop: bool
    jitter_s: int
    # Safety: minimum legal pause between checks, even if headers are silent.
    min_send_spacing_s: int = 180


class NSHTTPError(RuntimeError):
    """HTTP error while talking to NationStates API."""


class Storage:
    """SQLite-backed store for idempotence and light metadata."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn = sqlite3.connect(self._path, timeout=30)
        self._conn.executescript(_DB_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Closes the underlying SQLite connection."""
        self._conn.close()

    def already_sent(self, nation: str) -> bool:
        """Returns True if we have recorded a send to this nation."""
        cur = self._conn.execute(
            "SELECT 1 FROM sent_log WHERE nation = ? LIMIT 1", (nation,)
        )
        return cur.fetchone() is not None

    def record_send(self, nation: str, status: str, note: str) -> None:
        """Persists a send attempt outcome."""
        ts = int(time.time())
        self._conn.execute(
            "INSERT OR REPLACE INTO sent_log (nation, sent_ts, status, note) "
            "VALUES (?, ?, ?, ?)",
            (nation, ts, status, note[:500]),
        )
        self._conn.commit()

    def last_send_epoch(self) -> int | None:
        """Returns epoch seconds of the most recent successful send."""
        cur = self._conn.execute(
            "SELECT MAX(sent_ts) FROM sent_log WHERE status = 'sent'"
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0])


class NSClient:
    """Minimal NationStates API client with header-aware backoff."""

    def __init__(self, user_agent: str) -> None:
        self._sess = requests.Session()
        self._sess.headers.update({"User-Agent": user_agent})

    def close(self) -> None:
        """Close the HTTP session."""
        self._sess.close()

    def _respect_rate_headers(self, resp: requests.Response) -> None:
        """Sleep as instructed by API rate limiting headers, if present."""
        # Telegrams API uses X-Retry-After when you hit its stricter limit.
        x_retry = resp.headers.get("X-Retry-After")
        if x_retry:
            try:
                delay = int(x_retry)
            except ValueError:
                delay = 0
            if delay > 0:
                self._sleep_with_jitter(delay)
                return

        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                delay = int(retry_after)
            except ValueError:
                delay = 0
            if delay > 0:
                self._sleep_with_jitter(delay)
                return

        # If server exposes RateLimit-Reset, prefer that to be extra-kind!
        reset = resp.headers.get("RateLimit-Reset")
        remaining = resp.headers.get("RateLimit-Remaining")
        try:
            reset_s = int(reset) if reset is not None else 0
            remaining_i = int(remaining) if remaining is not None else 1
        except ValueError:
            reset_s, remaining_i = 0, 1

        if remaining_i <= 0 and reset_s > 0:
            self._sleep_with_jitter(reset_s)

    @staticmethod
    def _sleep_with_jitter(seconds: int) -> None:
        """Sleeps seconds plus positive jitter to avoid thundering herds."""
        jitter_range = max(1, int(seconds * 0.1))
        time.sleep(seconds + random.randint(0, jitter_range))

    def world_new_nations(self) -> list[str]:
        """Returns a list of very recently founded nation names."""
        params = {"q": "newnations"}
        resp = self._sess.get(_NS_API, params=params, timeout=30)
        if resp.status_code != 200:
            self._respect_rate_headers(resp)
            raise NSHTTPError(
                f"newnations HTTP {resp.status_code}: {resp.text[:200]}"
            )
        self._respect_rate_headers(resp)
        # XML is: <WORLD><NEWNATIONS>name1,name2,...</NEWNATIONS></WORLD>
        try:
            root = ET.fromstring(resp.text)
            nn = root.findtext(".//NEWNATIONS") or ""
        except ET.ParseError as exc:
            raise NSHTTPError(f"XML parse error: {exc}") from exc

        # The API returns comma-separated names (underscored).
        raw = [n.strip() for n in nn.split(",") if n.strip()]
        # Deduplicate while preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for n in raw:
            if n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
        return out

    def tg_can_recruit(self, nation: str, from_region: str) -> bool:
        """Checks whether a nation will accept a recruitment telegram.

        Respects general opt-outs as well as the
        "too soon from this region" rule via `from=REGION`.

        Args:
          nation: API nation name, underscores or spaces accepted.
          from_region: Region name for the `from` parameter.

        Returns:
          True iff sending a recruitment TG is currently permitted.
        """
        # Attach `from` specifically to the tgcanrecruit shard.
        params = {
            "nation": nation,
            "q": "tgcanrecruit;from=" + from_region,
        }
        resp = self._sess.get(_NS_API, params=params, timeout=30)
        if resp.status_code != 200:
            self._respect_rate_headers(resp)
            raise NSHTTPError(
                f"tgcanrecruit HTTP {resp.status_code}: {resp.text[:200]}"
            )
        self._respect_rate_headers(resp)
        try:
            root = ET.fromstring(resp.text)
            val = root.findtext(".//TGCANRECRUIT") or "0"
            return val.strip() == "1"
        except ET.ParseError as exc:
            raise NSHTTPError(f"XML parse error: {exc}") from exc

    def send_recruit_tg(
        self, client: str, tgid: str, secret: str, to_nation: str
    ) -> None:
        """Sends a single recruitment telegram to one nation.

        Honors Telegram API ratelimits via response headers.

        Args:
          client: Telegrams API Client Key (region-scoped).
          tgid: Telegram ID (from the composed message to tag:api).
          secret: Telegram Secret Key (keep private).
          to_nation: Recipient's nation name.

        Raises:
          NSHTTPError: On non-200 responses other than explicit backoff.
        """
        # GET is acceptable for the sendTG endpoint.
        params = {
            "a": "sendTG",
            "client": client,
            "tgid": tgid,
            "key": secret,
            "to": to_nation,
        }
        resp = self._sess.get(_SEND_TG, params=params, timeout=30)

        # Respect Telegram API-specific backoff first.
        if resp.status_code in (429, 423, 409):
            self._respect_rate_headers(resp)
            raise NSHTTPError(
                f"Backoff required, HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )

        if resp.status_code != 200:
            self._respect_rate_headers(resp)
            raise NSHTTPError(
                f"sendTG HTTP {resp.status_code}: {resp.text[:200]}"
            )

        # Even on success, be nice and read headers for global limits.
        self._respect_rate_headers(resp)


def _env(name: str, default: str | None = None) -> str:
    """Reads an environment variable or fails with a clear message."""
    val = os.getenv(name, default)
    if val is None or not str(val).strip():
        raise ValueError(
            f"Missing required environment variable: {name!r}."
        )
    return str(val).strip()


def _now_iso() -> str:
    """Returns an ISO8601 timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_nation(n: str) -> str:
    """Normalizes a nation name for storage and API use."""
    # NationStates accepts underscores/spaces interchangeably in API URLs.
    # Persist in canonical underscore form to avoid duplicates.
    return n.strip().replace(" ", "_")


def _sleep_until_next_allowed(
    storage: Storage, cfg: Config
) -> None:
    """Ensures we don't send more frequently than configured minimum.

    This complements the server-provided backoff. For example, if your
    region shares one Client Key among multiple tools, you may still
    get a 200 yet want extra spacing locally to coordinate.
    """
    last = storage.last_send_epoch()
    if last is None:
        return
    elapsed = int(time.time()) - last
    remain = max(0, cfg.min_send_spacing_s - elapsed)
    if remain > 0:
        # Add friendly jitter to avoid sync with other tools.
        jitter = random.randint(0, max(1, cfg.jitter_s))
        time.sleep(remain + jitter)


def _pick_candidates(
    all_new: Sequence[str],
    storage: Storage,
    max_take: int,
) -> list[str]:
    """Selects a deduped slice of new nations we haven't messaged yet."""
    out: list[str] = []
    for raw in all_new:
        nrm = _normalize_nation(raw)
        if storage.already_sent(nrm):
            continue
        out.append(nrm)
        if len(out) >= max_take:
            break
    return out


def run_once(cfg: Config, ns: NSClient, storage: Storage) -> None:
    """Runs one discovery + (optional) send cycle."""
    # 1) Discover fresh targets.
    try:
        fresh = ns.world_new_nations()
    except NSHTTPError as e:
        print(f"[{_now_iso()}] ERROR discovery: {e}", file=sys.stderr)
        return

    if not fresh:
        print(f"[{_now_iso()}] INFO no new nations discovered.")
        return

    candidates = _pick_candidates(fresh, storage, cfg.max_candidates)
    if not candidates:
        print(f"[{_now_iso()}] INFO nothing new to do.")
        return

    print(
        f"[{_now_iso()}] INFO considering {len(candidates)} candidate(s)."
    )

    # 2) For each candidate, check if we may recruit, then (maybe) send.
    for nation in candidates:
        # Respect local spacing between sends even if server would accept,
        # so we are extra kind and predictable!!
        _sleep_until_next_allowed(storage, cfg)

        # Check per-nation recruitment permission.
        try:
            ok = ns.tg_can_recruit(nation, cfg.region)
        except NSHTTPError as e:
            print(
                f"[{_now_iso()}] WARN tgcanrecruit failed for {nation}: {e}",
                file=sys.stderr,
            )
            # Record a soft failure to avoid tight loops on one bad name.
            storage.record_send(nation, "skipped", "tgcanrecruit_error")
            continue

        if not ok:
            print(
                f"[{_now_iso()}] INFO skip (not recruitable now): {nation}"
            )
            storage.record_send(nation, "skipped", "not_recruitable")
            continue

        if cfg.dry_run:
            print(
                f"[{_now_iso()}] DRY-RUN would send TG to: {nation}"
            )
            storage.record_send(nation, "dry_run", "preview_only")
            continue

        # 3) Attempt send; if the API says to wait, we back off and retry
        #    next loop iteration (we still record our attempt).
        try:
            ns.send_recruit_tg(
                cfg.tg_client, cfg.tg_id, cfg.tg_secret, nation
            )
            print(f"[{_now_iso()}] SENT recruitment TG to: {nation}")
            storage.record_send(nation, "sent", "ok")
        except NSHTTPError as e:
            # Likely a backoff header or an HTTP error; we record and proceed.
            print(
                f"[{_now_iso()}] WARN sendTG failed for {nation}: {e}",
                file=sys.stderr,
            )
            storage.record_send(nation, "failed", f"{e}")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parses command-line arguments."""
    p = argparse.ArgumentParser(
        description="NationStates recruitment telegram bot"
    )
    p.add_argument(
        "--region",
        default=os.getenv("NS_REGION"),
        help="Recruiting region name "
             "(default: $NS_REGION).",
    )
    p.add_argument(
        "--user-agent",
        default=os.getenv("NS_USER_AGENT"),
        help="Descriptive User-Agent per NS rules "
             "(default: $NS_USER_AGENT).",
    )
    p.add_argument(
        "--tg-client",
        default=os.getenv("NS_TG_CLIENT"),
        help="Telegrams API Client Key (default: $NS_TG_CLIENT).",
    )
    p.add_argument(
        "--tg-id",
        default=os.getenv("NS_TG_ID"),
        help="Telegram ID (TGID) (default: $NS_TG_ID).",
    )
    p.add_argument(
        "--tg-secret",
        default=os.getenv("NS_TG_SECRET"),
        help="Telegram Secret Key (default: $NS_TG_SECRET).",
    )
    p.add_argument(
        "--storage",
        default=os.getenv("NS_STORAGE", "xoloitzcuintli.sqlite3"),
        help="SQLite path for state (default: xoloitzcuintli.sqlite3).",
    )
    p.add_argument(
        "--max-candidates",
        type=int,
        default=int(os.getenv("NS_MAX_CANDIDATES", "50")),
        help="Max fresh targets to evaluate per cycle (default: 50).",
    )
    p.add_argument(
        "--dry-run",
        type=lambda s: s.lower() in {"1", "true", "yes", "y"},
        default=os.getenv("NS_DRY_RUN", "false").lower()
        in {"1", "true", "yes", "y"},
        help="If true, do not actually send telegrams.",
    )
    p.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously (respecting API rate limits).",
    )
    p.add_argument(
        "--jitter-s",
        type=int,
        default=int(os.getenv("NS_JITTER_S", "7")),
        help="Seconds of random jitter added to sleeps (default: 7).",
    )
    p.add_argument(
        "--min-send-spacing-s",
        type=int,
        default=int(os.getenv("NS_MIN_SPACING_S", "180")),
        help="Client-side minimum spacing between sends in seconds "
             "(default: 180).",
    )
    return p.parse_args(argv)


def build_config(nsargs: argparse.Namespace) -> Config:
    """Constructs a validated Config from args/env."""
    region = nsargs.region or _env("NS_REGION")
    user_agent = nsargs.user_agent or _env("NS_USER_AGENT")
    tg_client = nsargs.tg_client or _env("NS_TG_CLIENT")
    tg_id = nsargs.tg_id or _env("NS_TG_ID")
    tg_secret = nsargs.tg_secret or _env("NS_TG_SECRET")

    if (len(user_agent) < 8
                or ("@" not in user_agent and "http" not in user_agent)):
        raise ValueError(
            "User-Agent must allow moderators to contact you. Include an "
            "email or URL. Example: ExampleRecruiter/1.0 "
            "(contact: you@example.com)"
        )

    return Config(
        region=region,
        user_agent=user_agent,
        tg_client=tg_client,
        tg_id=tg_id,
        tg_secret=tg_secret,
        storage_path=nsargs.storage,
        max_candidates=max(1, int(nsargs.max_candidates)),
        dry_run=bool(nsargs.dry_run),
        loop=bool(nsargs.loop),
        jitter_s=max(0, int(nsargs.jitter_s)),
        min_send_spacing_s=max(1, int(nsargs.min_send_spacing_s)),
    )


def main(argv: Sequence[str]) -> None:
    """Entrypoint."""
    args = parse_args(argv)
    try:
        cfg = build_config(args)
    except ValueError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    storage = Storage(cfg.storage_path)
    client = NSClient(cfg.user_agent)
    try:
        if not cfg.loop:
            run_once(cfg, client, storage)
            return

        # Continuous mode: discover, evaluate, maybe send, then nap a bit.
        # Keep the loop polite by mixing short discovery naps with
        # server-directed backoff during send attempts.
        while True:
            run_once(cfg, client, storage)
            # Small idle pause between discovery passes to avoid hammering
            # the `newnations` shard.
            idle = random.randint(20, 45)
            time.sleep(idle)
    finally:
        client.close()
        storage.close()


if __name__ == "__main__":
    main(sys.argv[1:])
