# === CFP Crawler / Streamlit Dashboard (full version) ===
# 🔄  PLEASE DO NOT EDIT PARTIAL SEGMENTS; KEEP WHOLE FILE IN SYNC.
# -----------------------------------------------------------------------------
"""
CFP Dashboard & GitHub Actions Exporter  
🆕 **2025‑06‑12 – Cloud‑generate & Local‑consume edition**

This *single* Python file supports two modes:

1. **Exporter mode** – run in CI / GitHub Actions
   ```bash
   python cfp_crawler.py --export-json data.json
   ```
   Crawls Elsevier, Wiley, MDPI (JSON APIs) → writes `data.json`.

2. **Dashboard mode** – local Streamlit GUI
   ```bash
   streamlit run cfp_crawler.py
   ```
   If env var `REMOTE_JSON_URL` *or* sidebar “🌐 Use remote data.json” is ON →
   reads JSON (fast), else live‑crawl (needs open Internet).

Minimal GitHub Actions workflow (`.github/workflows/cfp-export.yml`):
```yaml
name: Export CFP JSON
on:
  schedule:
    - cron: '0 */6 * * *'
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.11'}
      - run: pip install feedparser requests
      - run: python cfp_crawler.py --export-json data.json
      - uses: stefanzweifel/git-auto-commit-action@v5
        with: {commit_message: 'chore(data): update CFP JSON'}
```
Raw JSON URL ⇒ `https://raw.githubusercontent.com/<user>/<repo>/main/data.json`
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Iterable, List, Optional

import pandas as pd
import requests

IS_DASHBOARD = "streamlit" in sys.argv[0]
if IS_DASHBOARD:
    import streamlit as st

try:
    import feedparser  # RSS/Atom parser
except ImportError:
    sys.exit("Missing dependency 'feedparser'. Run `pip install feedparser`.")

from requests.exceptions import SSLError, RequestException

###############################################################################
# Data structures
###############################################################################

@dataclass
class CFP:
    provider: str
    journal: str
    title: str
    description: str
    posted: Optional[dt.date]
    deadline: Optional[dt.date]
    link: str
    sjr: Optional[float] = None

    def to_dict(self):
        d = asdict(self)
        if self.posted:
            d["posted"] = self.posted.isoformat()
        if self.deadline:
            d["deadline"] = self.deadline.isoformat()
        return d

###############################################################################
# Helpers
###############################################################################

_REQUEST_DELAY = 1.0
_SESSION = requests.Session()
_DEADLINE_PATTERN = re.compile(r"(\b\d{1,2}\s?[A-Z][a-z]+\s?\d{4}\b)")
_MONTHS = "January February March April May June July August September October November December".split()
_MONTH_MAP = {m: i for i, m in enumerate(["", * _MONTHS])}
_SCIMAGO_API = "https://www.scimagojr.com/journalrank.php?out=json&search={q}"


def _get(url: str) -> Optional[requests.Response]:
    time.sleep(_REQUEST_DELAY)
    try:
        r = _SESSION.get(url, timeout=20, headers={"User-Agent": "CFPBot/0.6"})
        r.raise_for_status()
        return r
    except SSLError:
        try:
            r = _SESSION.get(url, timeout=20, headers={"User-Agent": "CFPBot/0.6"}, verify=False)
            r.raise_for_status()
            return r
        except Exception:
            return None
    except RequestException:
        return None


def _parse_date(text: str) -> Optional[dt.date]:
    m = _DEADLINE_PATTERN.search(text or "")
    if not m:
        return None
    day, mon, year = m.group(0).split()
    return dt.date(int(year), _MONTH_MAP.get(mon, 0), int(day))


def _sjr_lookup(journal: str) -> Optional[float]:
    resp = _get(_SCIMAGO_API.format(q=requests.utils.quote(journal)))
    if not resp:
        return None
    try:
        data = resp.json()
        return float(data[0]["SJR"].replace(',', '.')) if data else None
    except Exception:
        return None

###############################################################################
# Scrapers
###############################################################################

class BaseScraper:
    provider: str

    def fetch(self) -> Iterable[CFP]:
        raise NotImplementedError

    def _warn(self, msg: str):
        if IS_DASHBOARD:
            st.warning(f"{self.provider}: {msg} — skipped.")
        else:
            print(f"[WARN] {self.provider}: {msg}")


class ElsevierScraper(BaseScraper):
    provider = "Elsevier"
    FEED = "https://api.journals.elsevier.com/special-issues?limit=100"

    def fetch(self):
        resp = _get(self.FEED)
        if not resp:
            self._warn("network error")
            return
        try:
            for it in resp.json().get("specialIssues", []):
                yield CFP(
                    provider=self.provider,
                    journal=it.get("journalTitle", "Elsevier Journal"),
                    title=it.get("title", "Untitled"),
                    description=it.get("description", "")[:200],
                    posted=None,
                    deadline=_parse_date(it.get("submissionDeadline")),
                    link=it.get("url", ""),
                )
        except ValueError:
            self._warn("bad JSON")


class Wileyscraper(BaseScraper):
    provider = "Wiley"
    FEED = "https://wol-prod-cfp-files.s3.amazonaws.com/v2/calls.json"

    def fetch(self):
        resp = _get(self.FEED)
        if not resp:
            self._warn("network error")
            return
        try:
            for item in resp.json():
                yield CFP(
                    provider=self.provider,
                    journal=item.get("journalTitle", "Wiley Journal"),
                    title=item.get("title", "Untitled"),
                    description=item.get("description", "")[:200],
                    posted=None,
                    deadline=_parse_date(item.get("deadline")),
                    link=item.get("url", ""),
                )
        except ValueError:
            self._warn("bad JSON")


class MDPIScraper(BaseScraper):
    provider = "MDPI"
    FEED = "https://www.mdpi.com/journal/{j}?format=cfp&limit=100"
    JOURNALS = ["foods", "nutrients", "metabolites"]

    def fetch(self):
        for j in self.JOURNALS:
            resp = _get(self.FEED.format(j=j))
            if not resp:
                self._warn(f"{j} network error")
                continue
            try:
                for it in resp.json().get("specialIssues", []):
                    yield CFP(
                        provider=self.provider,
                        journal=j.capitalize(),
                        title=it.get("title", "Untitled"),
                        description=it.get("description", "")[:200],
                        posted=None,
                        deadline=_parse_date(it.get("deadline")),
                        link=it.get("url", ""),
                    )
            except ValueError:
                self._warn(f"{j} bad JSON")


SCRAPERS = {
    "Elsevier": ElsevierScraper(),
    "Wiley": Wileyscraper(),
    "MDPI": MDPIScraper(),
}

###############################################################################
# Core crawl
###############################################################################

def crawl(selected: List[str], with_sjr: bool = True) -> List[CFP]:
    out: List[CFP] = []
    for name in selected:
        for cfp in SCRAPERS[name].fetch():
            if with_sjr:
                cfp.sjr = _sjr_lookup(cfp.journal)
            out.append(cfp)
    return out

###############################################################################
# CLI exporter
###############################################################################

def main_cli():
    ap = argparse.ArgumentParser(description="CFP crawler/exporter")
    ap.add_argument("--export-json", metavar="FILE", required=True)
    ap.add_argument("--providers", nargs="*", default=list(SCRAPERS.keys()))
    ap.add

