# === Call‑for‑Papers Crawler & Streamlit Dashboard (2025‑06‑12) ===
"""
Features
--------
* **Exporter (CI / CLI)**
  ```bash
  python cfp_crawler.py --export-json data.json [--no-sjr]
  ```
  Crawls Elsevier · Wiley · MDPI  →  writes a unified JSON file.

* **Dashboard (local / cloud)**
  ```bash
  streamlit run cfp_crawler.py          # reads LIVE or remote JSON
  ```
  * Toggle “🌐 Use remote data.json” in sidebar.
  * Default remote URL can be given via `REMOTE_JSON_URL` env var.

Design goals
------------
* **Robust** against API changes – each scraper prints item‑count to stdout.
* **Fast**  – default skips SJR lookup (opt‑in via `--sjr`).
* **Zero external state** – single file, standard libs + pandas/requests/feedparser.
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
import feedparser  # RSS/Atom
from requests.exceptions import SSLError, RequestException

###############################################################################
# Streamlit lazy import
###############################################################################
IS_DASHBOARD = "streamlit" in sys.argv[0]
if IS_DASHBOARD:
    import streamlit as st

###############################################################################
# Data model
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
# Helper utils
###############################################################################
_REQUEST_DELAY = 1.0
_SESSION = requests.Session()
_DEADLINE_PATTERN = re.compile(r"(\b\d{1,2}\s?(January|February|March|April|May|June|July|August|September|October|November|December)\s?\d{4}\b)")
_MONTH_MAP = {m: i for i, m in enumerate(["", *"January February March April May June July August September October November December".split()])}
_SCIMAGO_API = "https://www.scimagojr.com/journalrank.php?out=json&search={q}"


def _log(msg: str):
    if IS_DASHBOARD:
        st.write(msg)
    else:
        print(msg)


def _get(url: str) -> Optional[requests.Response]:
    time.sleep(_REQUEST_DELAY)
    try:
        r = _SESSION.get(url, timeout=15, headers={"User-Agent": "CFPBot/0.9"})
        r.raise_for_status()
        return r
    except SSLError:
        try:
            r = _SESSION.get(url, timeout=15, verify=False)
            r.raise_for_status()
            return r
        except Exception:
            return None
    except RequestException:
        return None


def _parse_date(text: str | None) -> Optional[dt.date]:
    if not text:
        return None
    m = _DEADLINE_PATTERN.search(text)
    if not m:
        return None
    day, mon, year = m.group(0).split()
    return dt.date(int(year), _MONTH_MAP[mon], int(day))


def _sjr_lookup(journal: str) -> Optional[float]:
    resp = _get(_SCIMAGO_API.format(q=requests.utils.quote(journal)))
    if not resp:
        return None
    try:
        data = resp.json()
        return float(data[0]["SJR"].replace(",", ".")) if data else None
    except Exception:
        return None

###############################################################################
# Base scraper
###############################################################################
class BaseScraper:
    provider: str = ""

    def fetch(self) -> Iterable[CFP]:
        raise NotImplementedError

    def _warn(self, msg: str):
        _log(f"[WARN] {self.provider}: {msg}")

###############################################################################
# Elsevier – JSON hits
###############################################################################
class ElsevierScraper(BaseScraper):
    provider = "Elsevier"
    FEED = "https://api.journals.elsevier.com/special-issues?limit=100"

    def fetch(self):
        r = _get(self.FEED)
        if not r:
            self._warn("network error")
            return
        try:
            for it in r.json().get("hits", []):
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

###############################################################################
# Wiley – JSON `calls` array
###############################################################################
class Wileyscraper(BaseScraper):
    provider = "Wiley"
    FEED = "https://wol-prod-cfp-files.s3.amazonaws.com/v2/calls.json"

    def fetch(self):
        r = _get(self.FEED)
        if not r:
            self._warn("network error")
            return
        try:
            for it in r.json().get("calls", []):
                yield CFP(
                    provider=self.provider,
                    journal=it.get("journalTitle", "Wiley Journal"),
                    title=it.get("title", "Untitled"),
                    description=it.get("description", "")[:200],
                    posted=None,
                    deadline=_parse_date(it.get("deadline")),
                    link=it.get("url", ""),
                )
        except ValueError:
            self._warn("bad JSON")

###############################################################################
# MDPI – fallback to RSS (robust against Cloudflare)
###############################################################################
class MDPIScraper(BaseScraper):
    provider = "MDPI"
    JOURNALS = ["foods", "nutrients", "metabolites"]  # add more if needed
    FEED = "https://www.mdpi.com/rss/journal/{j}"

    def fetch(self):
        for j in self.JOURNALS:
            feed = feedparser.parse(self.FEED.format(j=j))
            if not feed.entries:
                self._warn(f"{j} rss empty")
                continue
            for e in feed.entries:
                yield CFP(
                    provider=self.provider,
                    journal=j.capitalize(),
                    title=e.title,
                    description=e.summary[:200],
                    posted=None,
                    deadline=_parse_date(e.summary),
                    link=e.link,
                )

SCRAPERS = {"Elsevier": ElsevierScraper(), "Wiley": Wileyscraper(), "MDPI": MDPIScraper()}

###############################################################################
# Core crawl
###############################################################################

def crawl(selected: List[str], sjr: bool = False) -> List[CFP]:
    out: List[CFP] = []
    for name in selected:
        items = list(SCRAPERS[name].fetch())
        _log(f"{name}: {len(items)} items")
        for c in items:
            if sjr:
                c.sjr = _sjr_lookup(c.journal)
            out.append(c)
    return out

###############################################################################
# CLI exporter
###############################################################################

def main_cli():
    ap = argparse.ArgumentParser(description="CFP crawler → JSON exporter")
    ap.add_argument("--export-json", required=True)
    ap.add_argument("--providers", nargs="*", default=list(SCRAPERS.keys()))
    ap.add_argument("--sjr", action="store_true", help="include Scimago SJR lookup (slow)")
    args = ap.parse_args()

    data = [c.to_dict() for c in crawl(args.providers, args.sjr)]
    with open(args.export_json, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ Exported {len(data)} CFP entries to {args.export_json}")

###############################################################################
# Streamlit dashboard
###############################################################################

def run_dashboard():
    st.set_page_config(page_title="CFP Dashboard", layout="wide")
    st.title("📢 Call‑for‑Papers Dashboard")

    remote_default = os.getenv("REMOTE_JSON_URL", "")

    with st.sidebar:
        st.header("Data source")
        use_remote = st.toggle("🌐 Use remote data.json", value=bool(remote_default))
        remote_url = st.text_input("Remote JSON URL", value=remote_default)
        if st.button("🔄 Refresh (live crawl)"):
            st.session_state.pop("cfp_data", None)

    if use_remote and remote_url:
        try:
            df = pd.read_json(remote_url)
        except Exception as e:
            st.error(f"Failed to load remote JSON: {e}")
            df = pd.DataFrame()
    else:
        if "cfp_data" not in st.session_state:
            with st.spinner("Live crawling …"):
                st.session_state["cfp_data"] = [c.to_dict() for c in crawl(list
