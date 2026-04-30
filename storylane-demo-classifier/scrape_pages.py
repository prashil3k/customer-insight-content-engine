#!/usr/bin/env python3
"""
Scrape demo links from the Storylane customer showcase and DemoDundies pages.
Adds all discovered demos to the persistent index.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

PROJECT_DIR = Path(__file__).parent

# Import index functions
import sys
sys.path.insert(0, str(PROJECT_DIR))
from run import load_index, save_index, merge_demo_into_index, generate_report

PAGES = [
    {
        "url": "https://www.storylane.io/customer-showcase",
        "source": "showcase",
        "label": "Customer Showcase",
    },
    {
        "url": "https://www.storylane.io/demodundies",
        "source": "demodundies",
        "label": "DemoDundies",
    },
]


async def scrape_all_pages():
    from playwright.async_api import async_playwright

    index = load_index()
    existing_count = len(index["demos"])
    print(f"Starting index has {existing_count} demos.\n")

    total_added = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})

        for page_config in PAGES:
            url = page_config["url"]
            source = page_config["source"]
            label = page_config["label"]

            print(f"{'='*60}")
            print(f"Scraping: {label}")
            print(f"URL: {url}")
            print(f"{'='*60}")

            page = await context.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)

                # Scroll to load all content (some pages lazy-load)
                await page.evaluate("""async () => {
                    for (let i = 0; i < 10; i++) {
                        window.scrollBy(0, window.innerHeight);
                        await new Promise(r => setTimeout(r, 500));
                    }
                    window.scrollTo(0, 0);
                }""")
                await page.wait_for_timeout(2000)

                # Extract all links and demo references from the page
                results = await page.evaluate("""() => {
                    const demos = [];
                    const seen = new Set();

                    // 1. Links to showcase detail pages
                    document.querySelectorAll('a[href*="/customer-showcase/"]').forEach(a => {
                        const href = a.href;
                        if (seen.has(href) || href === window.location.href) return;
                        seen.add(href);
                        const card = a.closest('[class*="card"], [class*="Card"], div') || a;
                        const nameEl = card.querySelector('h2, h3, h4, [class*="name"], [class*="Name"], [class*="title"], [class*="Title"]');
                        const name = nameEl ? nameEl.textContent.trim() : href.split('/').pop().replace(/-/g, ' ');
                        const catEl = card.querySelector('[class*="category"], [class*="tag"], [class*="industry"]');
                        const category = catEl ? catEl.textContent.trim() : '';
                        demos.push({ name, showcase_url: href, category, type: 'showcase_link' });
                    });

                    // 2. Links to demodundies detail pages
                    document.querySelectorAll('a[href*="/demodundies/"]').forEach(a => {
                        const href = a.href;
                        if (seen.has(href) || href === window.location.href) return;
                        seen.add(href);
                        const card = a.closest('[class*="card"], [class*="Card"], div') || a;
                        const nameEl = card.querySelector('h2, h3, h4, [class*="name"], [class*="Name"], [class*="title"], [class*="Title"]');
                        const name = nameEl ? nameEl.textContent.trim() : href.split('/').pop().replace(/-/g, ' ');
                        demos.push({ name, showcase_url: href, category: 'DemoDundies', type: 'demodundies_link' });
                    });

                    // 3. Direct demo iframes on the page
                    document.querySelectorAll('iframe').forEach(iframe => {
                        try {
                            const url = new URL(iframe.src);
                            if (url.pathname.includes('/demo/') || url.pathname.includes('/share/')) {
                                if (!seen.has(iframe.src)) {
                                    seen.add(iframe.src);
                                    demos.push({ name: '', demo_url: iframe.src, type: 'iframe' });
                                }
                            }
                        } catch(e) {}
                    });

                    // 4. Links directly to demo/share URLs
                    document.querySelectorAll('a').forEach(a => {
                        try {
                            const href = a.href;
                            if (seen.has(href)) return;
                            if (href.includes('/demo/') || href.includes('/share/')) {
                                if (href.includes('storylane.io') || href.includes('storylane.') ||
                                    href.includes('demo.') || href.includes('tour.')) {
                                    seen.add(href);
                                    const text = a.textContent.trim();
                                    const name = text && text.length < 80 ? text : '';
                                    demos.push({ name, demo_url: href, type: 'direct_link' });
                                }
                            }
                        } catch(e) {}
                    });

                    return demos;
                }""")

                print(f"   Found {len(results)} items on page")

                added = 0
                for item in results:
                    entry = {"source": source}

                    if item.get("showcase_url"):
                        entry["showcase_url"] = item["showcase_url"]
                    if item.get("demo_url"):
                        entry["demo_url"] = item["demo_url"]
                    if item.get("name"):
                        entry["name"] = item["name"]
                    else:
                        # Derive name from URL
                        url_for_name = item.get("demo_url", "") or item.get("showcase_url", "")
                        name = url_for_name.rstrip("/").split("/")[-1]
                        name = name.replace("-", " ").replace("_", " ").title()
                        entry["name"] = name
                    if item.get("category"):
                        entry["category"] = item["category"]

                    was_new = merge_demo_into_index(index, entry)
                    if was_new:
                        added += 1
                        print(f"      + {entry.get('name', '?')}")

                total_added += added
                print(f"   Added {added} new demos from {label}")

            except Exception as e:
                print(f"   Error scraping {label}: {e}")
            finally:
                await page.close()

        # Now visit each showcase detail page to extract the actual demo iframe URLs
        # for entries that have a showcase_url but no demo_url
        need_extraction = [
            (k, d) for k, d in index["demos"].items()
            if d.get("showcase_url") and not d.get("demo_url")
        ]

        if need_extraction:
            print(f"\n{'='*60}")
            print(f"Extracting demo URLs from {len(need_extraction)} detail pages")
            print(f"{'='*60}")

            page = await context.new_page()
            extracted = 0

            for i, (key, demo_entry) in enumerate(need_extraction):
                name = demo_entry.get("name", "?")
                showcase_url = demo_entry["showcase_url"]
                print(f"   [{i+1}/{len(need_extraction)}] {name}...", end=" ")

                try:
                    await page.goto(showcase_url, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(2000)

                    result = await page.evaluate("""() => {
                        const iframes = document.querySelectorAll('iframe');
                        let demoUrl = '';
                        for (const iframe of iframes) {
                            try {
                                const url = new URL(iframe.src);
                                if (url.pathname.includes('/demo/') || url.pathname.includes('/share/')) {
                                    demoUrl = iframe.src;
                                    break;
                                }
                            } catch(e) {}
                        }
                        // Also check for direct demo links
                        if (!demoUrl) {
                            const links = document.querySelectorAll('a');
                            for (const a of links) {
                                try {
                                    if ((a.href.includes('/demo/') || a.href.includes('/share/')) &&
                                        (a.href.includes('storylane') || a.href.includes('demo.') || a.href.includes('tour.'))) {
                                        demoUrl = a.href;
                                        break;
                                    }
                                } catch(e) {}
                            }
                        }
                        let livePreviewUrl = '';
                        document.querySelectorAll('a').forEach(a => {
                            if (a.textContent.includes('View live') || a.textContent.includes('Live preview')) {
                                livePreviewUrl = a.href;
                            }
                        });
                        const forms = document.querySelectorAll('form, [class*="gate"], [class*="leadCapture"]');
                        return { demoUrl, livePreviewUrl, isGated: forms.length > 0 };
                    }""")

                    if result["demoUrl"]:
                        demo_entry["demo_url"] = result["demoUrl"]
                        if result["livePreviewUrl"]:
                            demo_entry["live_preview_url"] = result["livePreviewUrl"]
                        demo_entry["is_gated"] = result["isGated"]
                        index["demos"][key] = demo_entry
                        extracted += 1
                        print(f"OK -> {result['demoUrl'][:60]}")
                    else:
                        demo_entry["is_accessible"] = False
                        demo_entry["error"] = "No demo iframe/link found"
                        index["demos"][key] = demo_entry
                        print(f"No demo found")

                except Exception as e:
                    print(f"Error: {str(e)[:60]}")

            await page.close()
            print(f"\n   Extracted {extracted} demo URLs from detail pages")

        await browser.close()

    save_index(index)
    generate_report(index)

    final_count = len(index["demos"])
    print(f"\n{'='*60}")
    print(f"SCRAPE COMPLETE")
    print(f"   Started with: {existing_count} demos")
    print(f"   Added: {total_added} new demos from page scraping")
    print(f"   Final index: {final_count} demos")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(scrape_all_pages())
