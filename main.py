import os
import asyncio
import httpx
from httpx import HTTPStatusError  # Import specific error for better catching

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

load_dotenv()  # Load .env file

# Use environment variable directly. Handle case where it might be missing
API_KEY = os.environ.get("GOOGLE_API_KEY")

app = FastAPI()

# --- Configuration ---
origins = [
    "http://localhost:3000",  # your local Next.js frontend
    "https://your-frontend-domain.com",  # your deployed frontend
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AuditRequest(BaseModel):
    url: str
    max_pages: int = 20
    concurrency: int = 2  # number of pages crawled in parallel


# --- Core Logic Functions ---

async def fetch_pagespeed(url: str):
    """
    Fetches PageSpeed Insights metrics with robust error handling.
    Returns scores as integers (0-100) on success, or "NA" strings on failure,
    along with an explicit 'error' message.
    """
    if not API_KEY:
        error_msg = "GOOGLE_API_KEY is not set in environment variables."
        print(f"PAGESPEED ERROR: {error_msg}")
        return {
            "performance": "NA",
            "accessibility": "NA",
            "seo": "NA",
            "error": error_msg
        }

    api_url = (
        "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
        f"?url={url}&strategy=mobile&category=performance&category=accessibility&category=seo&key={API_KEY}"
    )

    # Default metrics to return if any failure occurs
    failed_metrics = {
        "performance": "NA",
        "accessibility": "NA",
        "seo": "NA",
        "error": None
    }

    try:
        # Use a longer timeout for the PageSpeed API call as it can take a while (up to 60s)
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(api_url)

            # CRITICAL FIX: Check for HTTP error status codes (4xx or 5xx)
            resp.raise_for_status()

            data = resp.json()

        # Print raw response to logs for debugging successful 200 responses
        print(f"PAGESPEED RAW RESPONSE for {url}: {data}")

        # Check for API-level errors (API returns 200 but has an 'error' field)
        if data.get("error"):
            error_details = data["error"].get("message", "API returned an unknown error.")
            print(f"PAGESPEED API ERROR for {url}: {error_details}")
            return failed_metrics | {"error": f"PSI API Error: {error_details}"}

        lighthouse = data.get("lighthouseResult", {})
        categories = lighthouse.get("categories", {})

        # Ensure core data structure exists
        if not categories:
            error_details = "Incomplete Lighthouse data in successful response."
            print(f"PAGESPEED DATA MISSING for {url}: {error_details}")
            return failed_metrics | {"error": error_details}

        # Extraction logic remains the same, ensuring scores are converted to 0-100 integers
        return {
            "performance": int((categories.get("performance", {}).get("score", 0) or 0) * 100),
            "accessibility": int((categories.get("accessibility", {}).get("score", 0) or 0) * 100),
            "seo": int((categories.get("seo", {}).get("score", 0) or 0) * 100),
            "error": None  # Success
        }

    except HTTPStatusError as e:
        # Catches 400, 403 (Forbidden), 429 (Quota), etc.
        status_code = e.response.status_code
        error_msg = f"HTTP Error {status_code}. Check API Key/Quota. Response: {e.response.text[:100]}..."
        print(f"PAGESPEED HTTP ERROR for {url}: {error_msg}")
        return failed_metrics | {"error": error_msg}
    except Exception as e:
        # Catches JSON decoding errors, connection errors, timeouts, etc.
        error_msg = f"General fetch error: {type(e).__name__} - {e}"
        print(f"PAGESPEED GENERAL ERROR for {url}: {error_msg}")
        return failed_metrics | {"error": error_msg}


# --- API Endpoints ---

@app.get("/")
async def root():
    return {"message": "FastAPI audit backend is running!"}


@app.post("/audit")
async def audit_site(req: AuditRequest):
    visited = set()
    results = []
    queue = [req.url]
    semaphore = asyncio.Semaphore(req.concurrency)

    # Function now requires the persistent browser instance
    async def crawl(url: str, browser):
        async with semaphore:
            if url in visited or len(visited) >= req.max_pages:
                return
            visited.add(url)

            # Initialize page_data with default error status
            page_data = {
                "url": url,
                "status_code": None,
                "title": "",
                "meta_description": "",
                "h1": [],
                "internal_links": [],
                "error": None,  # General crawl error (Playwright)
                "pagespeed": {
                    "performance": "NA",
                    "accessibility": "NA",
                    "seo": "NA",
                    "error": "Not Yet Run"  # PageSpeed-specific error
                }
            }

            try:
                # 1. Playwright Scraping
                # Use the single, pre-launched browser instance to create a new page
                page = await browser.new_page()

                response = await page.goto(url, timeout=30000)
                page_data["status_code"] = response.status if response else None

                page_data["title"] = await page.title() or ""

                # Meta description
                meta_desc = await page.eval_on_selector(
                    'meta[name="description"]',
                    'el => el ? el.getAttribute("content") : ""'
                )
                page_data["meta_description"] = meta_desc or ""

                # H1 tags
                h1_tags = await page.eval_on_selector_all(
                    "h1",
                    "nodes => nodes.map(n => n.innerText)"
                )
                page_data["h1"] = h1_tags

                # Internal links
                anchors = await page.query_selector_all("a[href]")
                internal_links = []
                for a in anchors:
                    href = await a.get_attribute("href")
                    # Basic check for internal links
                    if href and (href.startswith("/") or href.startswith(req.url)):
                        # Standardize URL for queue
                        base_url = req.url.rstrip("/")
                        full_url = base_url + href if href.startswith("/") else href

                        if full_url not in visited:
                            queue.append(full_url)
                        internal_links.append(full_url)

                page_data["internal_links"] = internal_links

                await page.close()  # Close the individual page, not the browser

            except PlaywrightTimeoutError:
                page_data["error"] = "Playwright Timeout (30s)"
                print(f"CRAWL ERROR for {url}: Playwright Timeout")
            except Exception as e:
                page_data["error"] = f"Crawl Failed: {str(e)}"
                print(f"CRAWL ERROR for {url}: {e}")

            # 2. PageSpeed Insights Metrics (runs even if crawl failed)
            pagespeed_metrics = await fetch_pagespeed(url)
            page_data["pagespeed"] = pagespeed_metrics

            results.append(page_data)
            print(f"Finished processing {url}. PageSpeed Error: {pagespeed_metrics.get('error')}")

    p = None
    browser = None
    try:
        # **NEW: Initialize Playwright and Browser ONCE**
        p = await async_playwright().start()
        browser = await p.chromium.launch()

        # Crawl queue asynchronously
        tasks = []
        while queue and len(visited) < req.max_pages:
            # Create tasks from the current queue, passing the single browser instance
            tasks = [crawl(url, browser) for url in list(queue)]
            queue.clear()

            # Await the tasks created in this iteration
            await asyncio.gather(*tasks)

    finally:
        # **NEW: Ensure browser and playwright are closed ONCE after all tasks finish**
        if browser:
            await browser.close()
        if p:
            await p.stop()

    return {"results": results}