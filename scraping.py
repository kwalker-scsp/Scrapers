# Master CLI runner / GitHub Actions trigger
import asyncio
import sys
from datetime import datetime, timedelta

import database
import pipeline


def build_isw_url(target_date: datetime) -> str:
    """ISW publishes each assessment under a date-slugged URL."""
    month_name = target_date.strftime("%B").lower()
    return (
        "https://understandingwar.org/research/russia-ukraine/"
        f"russian-offensive-campaign-assessment-{month_name}-"
        f"{target_date.day}-{target_date.year}/"
    )


async def execute_daily_scrape():
    print("Initializing Database Schemas...")
    database.init_db()
    print(f"Connected via: {database.ACTIVE_DSN_LABEL}")

    # ISW reports on the previous day's events, so target yesterday. If that
    # assessment is not published yet the crawl returns empty and the source is
    # reported as SKIPPED -- the watermark is left alone so the next run retries.
    yesterday = datetime.now() - timedelta(days=1)

    target_sources = [
        {
            "name": "ISW Russian Offensive Campaign Assessment",
            "url": build_isw_url(yesterday),
        },
        {
            "name": "CFR Global Conflict Tracker",
            "url": "https://www.cfr.org/global-conflict-tracker/conflict/conflict-ukraine",
        },
        {
            "name": "Covert Shores Naval Timeline",
            "url": "https://www.hisutton.com/Timeline-2022-Ukraine-Invasion-At-Sea.html",
        },
    ]

    print("\nStarting Scraper Execution Loop...")
    results = {}
    for source in target_sources:
        try:
            processed = await pipeline.run_pipeline_for_url(
                source["name"], source["url"]
            )
            results[source["name"]] = "OK" if processed else "SKIPPED"
        except Exception as e:
            results[source["name"]] = f"FAILED: {e}"
            print(f"[Pipeline Error] {source['name']}: {e}")
            try:
                database.set_watermark(source["name"], None, "FAILED")
            except Exception:
                pass

    print("\n--- Scrape Summary ---")
    for name, status in results.items():
        print(f"  {status:<10} {name}")

    failures = [n for n, s in results.items() if s.startswith("FAILED")]
    if failures:
        print(f"\nDaily Scrape completed with {len(failures)} failure(s).")
        return 1

    print("\nDaily Scrape Completed Successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(execute_daily_scrape()))
