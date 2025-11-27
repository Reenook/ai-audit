import os
import asyncio
from urllib.parse import urljoin, quote

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import httpx
from dotenv import load_dotenv

load_dotenv()  

API_KEY = os.environ["GOOGLE_API_KEY"]

app = FastAPI()

origins = [
    "http://localhost:3000",
    "https://audit-jet-five.vercel.app",
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
    concurrency: int = 5
    psi_concurrency: int = 2


psi_semaphore = asyncio.Semaphore(2)

async def fetch_pagespeed(url: str):
    """Fetch PageSpeed Insights metrics with throttling and error handling."""
    async with psi_semaphore:
        # Ensure URL is properly encoded
        encoded_url = quote(url, safe="")
        api_url = (
            f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
            f"?url={encoded_url}&strategy=mobile&category=performance&category=accessibility&category=seo&key={API_KEY}"
        )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(api_url)
                data = resp.json()

            if "error" in data:
                return {"error": data["error"].get("message", "Unknown PSI error")}

            lighthouse = data.get("lighthouseResult", {})
            categories = lighthouse.get("categories", {})

            return {
                "performance": int((categories.get("performance", {}).get("score", 0) or 0) * 100),
                "accessibility": int((categories.get("accessibility", {}).get("score", 0) or 0) * 100),
                "seo": int((categories.get("seo", {}).get("score", 0) or 0) * 100),
            }

        except Exception as e:
            return {"error": f"PSI fetch failed: {str(e)}"}

@app.get("/")
async def root():
    return {"message": "FastAPI audit backend is running!"}

@app.post("/audit")
async def audit_site(req: AuditRequest):
    visited = set()
    queue = asyncio.Queue()
    await queue.put(req.url)

    crawl_results = []
    crawl_semaphore = asyncio.Semaphore(req.concurrency)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def crawl():
            while len(visited) < req.max_pages and not queue.empty():
                url = await queue.get()

                async with crawl_semaphore:
                    if url in visited:
                        continue

                    visited.add(url)
                    page_data = {"url": url}

                    try:
                        context = await browser.new_context()
                        page = await context.new_page()

                        resp = await page.goto(url, timeout=30000)
                        page_data["status_code"] = resp.status if resp else None
                        page_data["title"] = await page.title()

                        # Meta description
                        desc = page.locator("meta[name='description']")
                        page_data["meta_description"] = await desc.get_attribute("content") or ""

                        # H1 tags
                        h1_loc = page.locator("h1")
                        count = await h1_loc.count()
                        page_data["h1"] = [await h1_loc.nth(i).inner_text() for i in range(count)]

                        # Internal links
                        internal_links = []
                        all_links = page.locator("a[href]")
                        link_count = await all_links.count()

                        for i in range(link_count):
                            href = await all_links.nth(i).get_attribute("href")
                            if not href:
                                continue
                            full = urljoin(url, href)

                            # Internal and not visited
                            if full.startswith(req.url) and full not in visited:
                                internal_links.append(full)
                                await queue.put(full)

                        page_data["internal_links"] = internal_links

                        await context.close()

                    except Exception as e:
                        page_data["error"] = str(e)

                    crawl_results.append(page_data)

        # Start crawl workers
        workers = [asyncio.create_task(crawl()) for _ in range(req.concurrency)]
        await asyncio.gather(*workers)

        await browser.close()



    async def fetch_and_attach_psi(page):
        url = page["url"]
        psi_result = await fetch_pagespeed(url)
        page["pagespeed"] = psi_result
        return page

    psi_tasks = [fetch_and_attach_psi(page) for page in crawl_results]
    final_results = await asyncio.gather(*psi_tasks)

    return {"results": final_results}
