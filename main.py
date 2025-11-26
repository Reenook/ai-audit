import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import httpx
import asyncio
from dotenv import load_dotenv

load_dotenv()  # Load .env file

API_KEY = os.environ["GOOGLE_API_KEY"]

app = FastAPI()


origins = [
    "http://localhost:3000",             # your local Next.js frontend
    "https://your-frontend-domain.com", # your deployed frontend
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],  # allow GET, POST, OPTIONS, etc.
    allow_headers=["*"],
)


class AuditRequest(BaseModel):
    url: str
    max_pages: int = 20
    concurrency: int = 3  # number of pages crawled in parallel


async def fetch_pagespeed(url: str):
    api_url = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url={url}&key={API_KEY}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(api_url)
        data = resp.json()
    lighthouse = data.get("lighthouseResult", {})
    categories = lighthouse.get("categories", {})
    return {
        "performance": categories.get("performance", {}).get("score", 0) * 100,
        "accessibility": categories.get("accessibility", {}).get("score", 0) * 100,
        "seo": categories.get("seo", {}).get("score", 0) * 100,
    }

# -----------------------------
# Routes
# -----------------------------
@app.get("/")
async def root():
    return {"message": "FastAPI audit backend is running!"}

@app.post("/audit")
async def audit_site(req: AuditRequest):
    visited = set()
    results = []
    queue = [req.url]
    semaphore = asyncio.Semaphore(req.concurrency)

    async def crawl(url: str):
        async with semaphore:
            if url in visited or len(visited) >= req.max_pages:
                return
            visited.add(url)
            page_data = {"url": url}

            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch()
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
                        if href and (href.startswith("/") or href.startswith(req.url)):
                            full_url = req.url.rstrip("/") + href if href.startswith("/") else href
                            if full_url not in visited:
                                queue.append(full_url)
                            internal_links.append(full_url)
                    page_data["internal_links"] = internal_links

                    # PageSpeed Insights metrics
                    pagespeed_metrics = await fetch_pagespeed(url)
                    page_data["pagespeed"] = pagespeed_metrics

                    await browser.close()

            except PlaywrightTimeoutError:
                page_data["error"] = "Timeout"
            except Exception as e:
                page_data["error"] = str(e)

            results.append(page_data)

    # Crawl queue asynchronously
    while queue and len(visited) < req.max_pages:
        tasks = [crawl(url) for url in list(queue)]
        queue.clear()
        await asyncio.gather(*tasks)

    return {"results": results}
