import os
import asyncio
from urllib.parse import urljoin, quote

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright
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


# Semaphores for throttling
psi_semaphore = asyncio.Semaphore(2)
link_check_semaphore = asyncio.Semaphore(5)


async def fetch_pagespeed(url: str):
    """Fetch PageSpeed Insights metrics with throttling and error handling."""
    async with psi_semaphore:
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


async def check_link(url: str) -> int | None:
    """Return HTTP status code or None if failed."""
    async with link_check_semaphore:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.head(url, follow_redirects=True)
                return resp.status_code
        except Exception:
            return None


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

                        # Navigate
                        resp = await page.goto(url, timeout=30000)
                        page_data["status_code"] = resp.status if resp else None

                        # Title
                        page_data["title"] = await page.title()
                        page_data["title_length"] = len(page_data["title"]) if page_data["title"] else 0

                        # Meta description
                        desc = page.locator("meta[name='description']")
                        meta_desc = await desc.get_attribute("content") or ""
                        page_data["meta_description"] = meta_desc
                        page_data["meta_description_length"] = len(meta_desc)

                        # H1, H2, H3 tags
                        h1_loc = page.locator("h1")
                        page_data["h1"] = [await h1_loc.nth(i).inner_text() for i in range(await h1_loc.count())]

                        h2_loc = page.locator("h2")
                        page_data["h2"] = [await h2_loc.nth(i).inner_text() for i in range(await h2_loc.count())]

                        h3_loc = page.locator("h3")
                        page_data["h3"] = [await h3_loc.nth(i).inner_text() for i in range(await h3_loc.count())]

                        # Images and alt attributes
                        imgs = page.locator("img")
                        images_info = []
                        for i in range(await imgs.count()):
                            src = await imgs.nth(i).get_attribute("src")
                            alt = await imgs.nth(i).get_attribute("alt") or ""
                            images_info.append({"src": src, "alt": alt, "has_alt": bool(alt.strip())})
                        page_data["images"] = images_info

                        # Detect CMS/platform
                        platform = "Unknown"
                        generator = await page.locator("meta[name='generator']").get_attribute("content")
                        html = await page.content()
                        if generator:
                            platform = generator
                        elif "/wp-content/" in html or "/wp-includes/" in html:
                            platform = "WordPress"
                        elif "cdn.shopify.com" in html:
                            platform = "Shopify"
                        elif "wix.com" in html or "wixsite.com" in html:
                            platform = "Wix"
                        elif "squarespace.com" in html:
                            platform = "Squarespace"
                        page_data["platform"] = platform

                        # Internal links and broken links
                        internal_links = []
                        links_info = []
                        all_links = page.locator("a[href]")

                        for i in range(await all_links.count()):
                            href = await all_links.nth(i).get_attribute("href")
                            if not href or href.startswith("#") or href.startswith("mailto:"):
                                continue
                            full = urljoin(url, href)

                            # Internal links
                            if full.startswith(req.url):
                                internal_links.append(full)
                                await queue.put(full)

                            # Broken link check
                            status = await check_link(full)
                            links_info.append({
                                "url": full,
                                "status": status,
                                "is_broken": status is None or status >= 400,
                                "internal": full.startswith(req.url)
                            })

                        page_data["internal_links"] = internal_links
                        page_data["links"] = links_info

                        await context.close()

                    except Exception as e:
                        page_data["error"] = str(e)

                    crawl_results.append(page_data)

        # Start crawl workers
        workers = [asyncio.create_task(crawl()) for _ in range(req.concurrency)]
        await asyncio.gather(*workers)
        await browser.close()

    # Fetch PageSpeed
    async def fetch_and_attach_psi(page):
        psi_result = await fetch_pagespeed(page["url"])
        page["pagespeed"] = psi_result
        return page

    psi_tasks = [fetch_and_attach_psi(page) for page in crawl_results]
    final_results = await asyncio.gather(*psi_tasks)

    return {"results": final_results}
