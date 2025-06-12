"""
CFP Dashboard & GitHub Actions Exporter
======================================
🆕 **2025‑06‑12 – Cloud‑generate & Local‑consume edition**

This *single* Python file supports **two modes**:

1. **Exporter mode** – run in CI / GitHub Actions
   ```bash
   python cfp_dashboard.py --export-json data.json
   ```
   * Crawls Call‑for‑Papers (Elsevier, Wiley, MDPI; easily extendable).
   * Writes a compact `data.json` (≈ 60–100 kB) to repo for anyone to fetch.

2. **Dashboard mode** – local Streamlit GUI
   ```bash
   streamlit run cfp_dashboard.py
   ```
   * If env var **`REMOTE_JSON_URL`** *or* sidebar toggle “🌐 Use remote data.json” is ON → **reads JSON** (fast, works behind firewall).
   * Otherwise performs live crawl (needs open Internet).

---
### Minimal GitHub Actions workflow (copy to `.github/workflows/cfp-export.yml`)
```yaml
name: Export CFP JSON
on:
  schedule:
    - cron:  '0 */6 * * *'   # every 6 h; adjust as needed
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.11'}
      - run: pip install feedparser requests
      - run: python cfp_dashboard.py --export-json data.json
      - uses: stefanzweifel/git-auto-commit-action@v5
        with: {commit_message: 'chore(data): update CFP JSON'}
```
Raw JSON URL example:
```
https://raw.githubusercontent.com/<user>/<repo>/main/data.json
```
Set it locally:
```bash
# Windows PowerShell
env:REMOTE_JSON_URL = "https://raw.githubusercontent.com/<user>/<repo>/main/data.json"
streamlit run cfp_dashboard.py
```

Author  : *your‑name*  •  Last update : 2025‑06‑12
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Iterable, List, Optional

import pandas as pd
import requests

# Only import Streamlit when launched via `streamlit run …` (reduces CI deps)
IS_DASHBOARD = "streamlit" in sys.argv[0]
if IS_DASHBOARD:
    import streamlit as st

try:
    import feedparser  # RSS/Atom parser
except ImportError as e:
    sys.exit("Missing dependency 'feedparser'. Run `pip install feedparser`. ")

from requests.exceptions import SSLError, RequestException

################################################################################
#                               DATA CLASSES                                   #
################################################################################

@dataclass
class CFP:
    provider: str
    journal: str
    title: str
    description: str
    posted: Optional[_dt.date]
    deadline: Optional[_dt.date]
    link: str
    sjr: Optional[float] = None

    def to_dict(self):
        d = asdict(self)
        if self.posted:
            d["posted"] = self.posted.isoformat()
        if self.deadline:
            d["deadline"] = self.deadline.isoformat()
        return d

################################################################################
#                              HELPER FUNCTIONS                                #
################################################################################

_REQUEST_DELAY = 1.0
_SESSION = requests.Session()
_DEADLINE_PATTERN = re.compile(r"(\b\d{1,2}\s?[A-Z][a-z]+\s?\d{4}\b)")
_MONTH_MAP = {m: i for i, m in enumerate(["", *"January February March April May June July August September October November December".split()])}
_SCIMAGO_API = "https://www.scimagojr.com/journalrank.php?out=json&search={q}"


def _get(url: str) -> Optional[requests.Response]:
    time.sleep(_REQUEST_DELAY)
    try:
        r = _SESSION.get(url, timeout=20, headers={"User-Agent": "CFPBot/0.5"})
        r.raise_for_status()
        return r
    except SSLError:
        try:
            r = _SESSION.get(url, timeout=20, headers={"User-Agent": "CFPBot/0.5"}, verify=False)
            r.raise_for_status()
            return r
        except Exception:
            return None
    except RequestException:
        return None


def _parse_date(text: str) -> Optional[_dt.date]:
    m = _DEADLINE_PATTERN.search(text)
    if not m:
        return None
    day, mon, year = m.group(0).split()
    return _dt.date(int(year), _MONTH_MAP[mon], int(day))


def _sjr_lookup(journal: str) -> Optional[float]:
    resp = _get(_SCIMAGO_API.format(q=requests.utils.quote(journal)))
    if not resp:
        return None
    try:
        data = resp.json()
        return float(data[0]["SJR"].replace(",", ".")) if data else None
    except Exception:
        return None

################################################################################
#                                SCRAPERS                                      #
################################################################################

class BaseScraper:
    provider = "Base"

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
            data = resp.json().get("specialIssues", [])
        except ValueError:
            self._warn("bad JSON")
            return
        for it in data:
            yield CFP(
                provider=self.provider,
                journal=it.get("journalTitle", "Elsevier Journal"),
                title=it.get("title", "Untitled"),
                description=it.get("description", "")[:200],
                posted=None,
                deadline=_parse_date(it.get("submissionDeadline", "")),
                link=it.get("url", ""),
            )


class Wileyscraper(BaseScraper):
    provider = "Wiley"
    FEED = "https://wol-prod-cfp-files.s3.amazonaws.com/v2/calls.json"  # static JSON

    def fetch(self):
        resp = _get(self.FEED)
        if not resp:
            self._warn("network error")
            return
        try:
            data = resp.json()
        except ValueError:
            self._warn("bad JSON")
            return
        for item in data:
            yield CFP(
                provider=self.provider,
                journal=item.get("journalTitle", "Wiley Journal"),
                title=item.get("title", "Untitled"),
                description=item.get("description", "")[:200],
                posted=None,
                deadline=_parse_date(item.get("deadline", "")),
                link=item.get("url", ""),
            )


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
                data = resp.json().get("specialIssues", [])
            except ValueError:
                self._warn(f"{j} bad JSON")
                continue
            for it in data:
                yield CFP(
                    provider=self.provider,
                    journal=j.capitalize(),
                    title=it.get("title", "Untitled"),
                    description=it.get("description", "")[:200],
                    posted=None,
                    deadline=_parse_date(it.get("deadline", "")),
                    link
