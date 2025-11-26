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
    "https://audit-jad4f5t9n-reenooks-projects.vercel.app/",
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
    concurrency: int = 2
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
    results = []
    queue = [req.url]
    crawl_semaphore = asyncio.Semaphore(req.concurrency)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def crawl(url: str):
            async with crawl_semaphore:
                if url in visited or len(visited) >= req.max_pages:
                    return
                visited.add(url)
                page_data = {"url": url}

                try:
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
                        if href:
                            full_url = urljoin(url, href)
                            if full_url.startswith(req.url) and full_url not in visited:
                                queue.append(full_url)
                                internal_links.append(full_url)
                    page_data["internal_links"] = internal_links

                    # PageSpeed Insights metrics
                    pagespeed_metrics = await fetch_pagespeed(url)
                    page_data["pagespeed"] = pagespeed_metrics

                    await page.close()

                except PlaywrightTimeoutError:
                    page_data["error"] = "Timeout"
                except Exception as e:
                    page_data["error"] = str(e)

                results.append(page_data)

        # Crawl queue incrementally to respect concurrency
        while queue and len(visited) < req.max_pages:
            batch = [queue.pop(0) for _ in range(min(req.concurrency, len(queue)))]
            await asyncio.gather(*(crawl(u) for u in batch))

        await browser.close()

    return {"results": results}
