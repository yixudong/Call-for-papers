# === Call‑for‑Papers Crawler & Streamlit Dashboard (stable v1 • 2025‑06‑12) ===
"""
Single‑file utility with two modes:

• **Exporter (CI / CLI)**
  ```bash
  python cfp_crawler.py --export-json data.json [--sjr]
  ```
  Crawls Elsevier · Wiley · MDPI → writes a unified JSON file.

• **Dashboard (local / cloud)**
  ```bash
  streamlit run cfp_crawler.py
  ```
  Toggle “🌐 Use remote data.json” in sidebar. Remote URL can be preset with
  `REMOTE_JSON_URL` env var.
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
# Streamlit lazy‑import (only when dashboard mode)                             #
###############################################################################
IS_DASHBOARD = "streamlit" in sys.argv[0]
if IS_DASHBOARD:
    import streamlit as st

###############################################################################
# Data model                                                                   #
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
# Helpers                                                                     #
###############################################################################
_REQUEST_DELAY = 1.0
_SESSION = requests.Session()
_DEADLINE_PATTERN = re.compile(r"\b(\d{1,2})\s?(January|February|March|April|May|June|July|August|September|October|November|December)\s?(\d{4})\b")
_MONTHS = "January February March April May June July August September October November December".split()
_MONTH_MAP = {m: i for i, m in enumerate(["", *_MONTHS])}
_SCIMAGO_API = "https://www.scimagojr.com/journalrank.php?out=json&search={q}"


def _log(msg: str):
    if IS_DASHBOARD:
        st.write(msg)
    else:
        print(msg)


def _get(url: str) -> Optional[requests.Response]:
    """HTTP GET with retry/SSL fallback + debug log"""
    time.sleep(_REQUEST_DELAY)
    try:
        r = _SESSION.get(
            url,
            timeout=20,
            headers={
                # 伪装成正常浏览器，减少 403 / 503
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0 Safari/537.36 CFPBot",
                "Accept": "application/json, text/plain,*/*",
            },
        )
        _log(f"GET {url[:70]}… → {r.status_code}")
        r.raise_for_status()
        return r
    except SSLError:
        # 一次性关闭 SSL 验证再试
        try:
            r = _SESSION.get(url, timeout=20, verify=False)
            _log(f"GET {url[:70]}… (no-SSL) → {r.status_code}")
            r.raise_for_status()
            return r
        except Exception:
            return None
    except RequestException as e:
        _log(f"GET {url[:70]}… failed: {e}")
        return None


def _parse_date(text: str | None) -> Optional[dt.date]:
    if not text:
        return None
    m = _DEADLINE_PATTERN.search(text)
    if not m:
        return None
    day, mon, year = m.groups()
    return dt.date(int(year), _MONTH_MAP.get(mon, 0), int(day))


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
# Base scraper                                                                #
###############################################################################
class BaseScraper:
    provider: str = ""

    def fetch(self) -> Iterable[CFP]:
        raise NotImplementedError

    def _warn(self, msg: str):
        _log(f"[WARN] {self.provider}: {msg}")

###############################################################################
# Elsevier – JSON hits                                                        #
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
            self._warn("bad JSON structure")

###############################################################################
# Wiley – JSON `calls` array                                                  #
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
            self._warn("bad JSON structure")

###############################################################################
# MDPI – JSON API first, RSS fallback                                         #
###############################################################################
class MDPIScraper(BaseScraper):
    provider = "MDPI"
    JOURNALS = [
        # use lowercase slugs shown in MDPI URLs
        "mathematics", "ecologies", "ijerph", "materials", "ijfs",
        "sensors", "risks", "molecules", "geometry", "plants", "cells",
    ]

    # MDPI’s hidden JSON.  No `status=open` → returns both current & upcoming CFPs
    JSON_API = "https://www.mdpi.com/journal/{j}?format=cfp&limit=300"
    # Generic article RSS (used as fallback)
    RSS = "https://www.mdpi.com/rss/journal/{j}"

    def fetch(self):
        for j in self.JOURNALS:
            # ◼︎ 1) Try official Special‑Issue JSON
            r = _get(self.JSON_API.format(j=j))
            if r:
                try:
                    data = r.json().get("specialIssues", [])
                    if data:  # got hits ➜ yield & continue
                        for it in data:
                            yield CFP(
                                provider=self.provider,
                                journal=j.capitalize(),
                                title=it.get("title", "Untitled"),
                                description=it.get("description", "")[:200],
                                posted=None,
                                deadline=_parse_date(it.get("deadline")),
                                link=it.get("url", ""),
                            )
                        continue  # skip RSS fallback
                except Exception:
                    self._warn(f"{j} JSON decode error → fallback RSS")

            # ◼︎ 2) Fallback RSS — keep items whose link looks like an SI
            feed = feedparser.parse(self.RSS.format(j=j))
            for e in feed.entries:
                if "/special_issues/" in e.link or "/special-issue" in e.link:
                    yield CFP(
                        provider=self.provider,
                        journal=j.capitalize(),
                        title=e.title,
                        description=e.summary[:200],
                        posted=None,
                        deadline=_parse_date(e.summary),
                        link=e.link,
                    )

# Register all scrapers
SCRAPERS = {
    "Elsevier": ElsevierScraper(),
    "Wiley":    Wileyscraper(),
    "MDPI":     MDPIScraper(),
}

###############################################################################
# Core crawl                                                                 #
###############################################################################

def crawl(providers: List[str], sjr: bool = False) -> List[CFP]:
    """Iterate scrapers, optionally enriching with SJR."""
    results: List[CFP] = []
    for name in providers:
        items = list(SCRAPERS[name].fetch())
        _log(f"{name}: {len(items)} items")
        for c in items:
            if sjr:
                c.sjr = _sjr_lookup(c.journal)
            results.append(c)
    return results

###############################################################################
# CLI exporter                                                               #
###############################################################################

def main_cli():
    ap = argparse.ArgumentParser(description="CFP crawler → JSON exporter")
    ap.add_argument("--export-json", required=True, help="output JSON file path")
    ap.add_argument("--providers", nargs="*", default=list(SCRAPERS.keys()))
    ap.add_argument("--sjr", action="store_true", help="lookup Scimago SJR (slow)")
    args = ap.parse_args()

    data = [c.to_dict() for c in crawl(args.providers, args.sjr)]
    with open(args.export_json, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ Exported {len(data)} CFP entries → {args.export_json}")

###############################################################################
# Streamlit dashboard                                                        #
###############################################################################

def run_dashboard():
    st.set_page_config(page_title="CFP Dashboard", layout="wide")
    st.title("📢 Call‑for‑Papers Dashboard")

    remote_default = os.getenv("REMOTE_JSON_URL", "")

    with st.sidebar:
        st.header("Data source")
        use_remote = st.toggle("🌐 Use remote data.json", value=bool(remote_default))
        remote_url = st.text_input("Remote JSON URL", value=remote_default)
        refresh = st.button("🔄 Live crawl now")

    if use_remote and remote_url:
        try:
            df = pd.read_json(remote_url)
        except Exception as e:
            st.error(f"Failed to load remote JSON: {e}")
            df = pd.DataFrame()
    else:
        if refresh or "cfp_data" not in st.session_state:
            with st.spinner("Crawling …"):
                st.session_state["cfp_data"] = [c.to_dict() for c in crawl(list(SCRAPERS.keys()))]
        df = pd.DataFrame(st.session_state["cfp_data"])

    if df.empty:
        st.warning("No call‑for‑papers entries found.")
        return

    st.subheader(f"Results: {len(df)} CFPs")
    st.dataframe(df, height=560)

    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("💾 Download CSV", data=csv, file_name="cfp_results.csv", mime="text/csv")

###############################################################################
# Entry point                                                                #
###############################################################################

if __name__ == "__main__":
    if IS_DASHBOARD:
        run_dashboard()
    else:
        main_cli()
