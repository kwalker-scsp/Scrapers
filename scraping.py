# Master CLI runner / GitHub Actions trigger
import asyncio
from datetime import datetime, timedelta
import database
import pipeline

STATIC_TARGET_SOURCES = [
    {
        "name": "CFR Global Conflict Tracker",
        "url": "https://www.cfr.org/global-conflict-tracker/conflict/conflict-ukraine"
    },
    {
        "name": "Covert Shores Naval Timeline",
        "url": "https://www.hisutton.com/Timeline-2022-Ukraine-Invasion-At-Sea.html"
    }
]

def generate_isw_daily_url(target_date: datetime = None) -> dict:
    if target_date is None:
        target_date = datetime.now() - timedelta(days=1)
    
    month_name = target_date.strftime("%B").lower()
    day = target_date.strftime("%d").lstrip("0")
    year = target_date.strftime("%Y")
    
    url = f"https://understandingwar.org/research/russia-ukraine/russian-offensive-campaign-assessment-{month_name}-{day}-{year}/"
    return {
        "name": f"ISW Assessment ({target_date.strftime('%Y-%m-%d')})",
        "url": url
    }

async def execute_daily_scrape():
    print("Scraping🧙‍♂️")
    database.init_db()

    sources = list(STATIC_TARGET_SOURCES)
    
    yesterday = datetime.now() - timedelta(days=1)
    isw_target = generate_isw_daily_url(yesterday)
    sources.append(isw_target)

    for source in sources:
        await pipeline.run_pipeline_for_url(source["name"], source["url"])

if __name__ == "__main__":
    asyncio.run(execute_daily_scrape())