# -*- coding: utf-8 -*-
"""
Playwright crawler for multiple CFP sites.
配置写在 cfg.yaml，每站点声明:
  - name: Elsevier
    url:  https://www.sciencedirect.com/browse/calls-for-papers
    list_selector:  "article"        # CSS 选到每个卡片
    title_selector: "h3"
    journal_selector: "a[href*='/journal/']"
    link_selector:   "a[href*='call-for-papers']"
    deadline_selector: "time"        # 可缺省
    outfile: elsevier.json

只抓“静态”字段，deadline_raw 无需解析——让 dashboard 再处理。
"""
import asyncio, json, yaml, pathlib
from playwright.async_api import async_playwright

CFG = yaml.safe_load(open(pathlib.Path(__file__).with_name("cfg.yaml")))

async def scrape(site, pw):
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
    page = await browser.new_page()
    await page.goto(site["url"], timeout=0)
    await page.wait_for_selector(site["list_selector"])
    cards = await page.query_selector_all(site["list_selector"])
    print(f"{site['name']}: {len(cards)} cards")

    out = []
    for c in cards:
        title   = await c.query_selector_eval(site["title_selector"], "e=>e.textContent") if site.get("title_selector") else ""
        journal = await c.query_selector_eval(site["journal_selector"], "e=>e.textContent") if site.get("journal_selector") else ""
        link    = await c.eval_on_selector(site["link_selector"], "e=>e.href") if site.get("link_selector") else ""
        deadline= ""
        if site.get("deadline_selector"):
            deadline = await c.query_selector_eval(site["deadline_selector"], "e=>e.textContent") or ""
        out.append({
            "provider": site["name"],
            "journal": journal.strip(),
            "title":   title.strip(),
            "deadline_raw": deadline.strip(),
            "link":    link,
        })

    out_path = pathlib.Path(__file__).with_name(site["outfile"])
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"  ⤵  wrote {out_path}")
    await browser.close()

async def main():
    async with async_playwright() as pw:
        for site in CFG:
            await scrape(site, pw)

if __name__ == "__main__":
    asyncio.run(main())
