import asyncio
import aiohttp
import ssl
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import os
import json
import time
import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# ---------- CONFIG ----------
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

START_URL = "https://www.bbcgoodfood.com/recipes"
OUTPUT_FILE = os.path.join(DATA_DIR, "bbc_goodfood_mealie_import.txt")

BBC_REQUEST_DELAY = 1  # seconds
MAX_CONCURRENCY = 4

MEALIE_API_URL = "http://your_mealie_host:9000/api/recipes/import"
MEALIE_API_KEY = "YOUR_MEALIE_API_KEY"
MEALIE_RATE_LIMIT = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

visited_pages = set()
queued_pages = set()
found_recipes = set()
crawl_progress = {"pages":0, "recipes":0, "status":"idle"}
upload_progress = {"index":0, "total":0, "status":"idle"}

# ---------- Resume ----------
if os.path.exists(OUTPUT_FILE):
    with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            found_recipes.add(line.strip())

# ---------- Helpers ----------
def is_recipe_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc == "www.bbcgoodfood.com" and parsed.path.startswith("/recipes/") and parsed.path.count("/") >= 2

def is_internal(url: str) -> bool:
    return urlparse(url).netloc == "www.bbcgoodfood.com"

async def fetch(session, url):
    await asyncio.sleep(BBC_REQUEST_DELAY)
    try:
        async with session.get(url) as resp:
            if resp.status == 200 and "text/html" in resp.headers.get("Content-Type", ""):
                return await resp.text()
    except Exception:
        return None
    return None

def is_true_recipe(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and data.get("@type") == "Recipe":
                return True
            if isinstance(data, list) and any(isinstance(i, dict) and i.get("@type")=="Recipe" for i in data):
                return True
        except Exception:
            continue
    return False

# ---------- Crawl Worker ----------
async def worker(queue, session):
    while True:
        url = await queue.get()
        if url in visited_pages:
            queue.task_done()
            continue
        visited_pages.add(url)
        html = await fetch(session, url)
        crawl_progress["pages"] += 1
        if not html:
            queue.task_done()
            continue
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            link = urljoin(url, a["href"]).split("?")[0].split("#")[0]
            if is_recipe_url(link) and link not in found_recipes:
                recipe_html = await fetch(session, link)
                if recipe_html and is_true_recipe(recipe_html):
                    found_recipes.add(link)
                    crawl_progress["recipes"] = len(found_recipes)
                    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                        f.write(link+"\n")
            elif is_internal(link) and link not in visited_pages and link not in queued_pages:
                queued_pages.add(link)
                await queue.put(link)
        queue.task_done()

# ---------- Crawl Start ----------
@app.get("/start_crawl")
async def start_crawl():
    asyncio.create_task(crawl_bbc())
    return {"status":"started"}

async def crawl_bbc():
    crawl_progress["status"]="running"
    queue = asyncio.Queue()
    queue.put_nowait(START_URL)
    queued_pages.add(START_URL)
    headers = {"User-Agent": USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=30)
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_context, limit=MAX_CONCURRENCY)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
        workers = [asyncio.create_task(worker(queue, session)) for _ in range(MAX_CONCURRENCY)]
        await queue.join()
        for w in workers:
            w.cancel()
    crawl_progress["status"]="done"
    # after crawling, push to Mealie
    await push_mealie()

# ---------- Push to Mealie ----------
async def push_mealie():
    upload_progress["status"]="running"
    if not os.path.exists(OUTPUT_FILE):
        upload_progress["status"]="done"
        return
    headers = {"Authorization": f"Bearer {MEALIE_API_KEY}", "Content-Type": "application/json"}
    with open(OUTPUT_FILE,"r",encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]
    upload_progress["total"] = len(urls)
    for idx,url in enumerate(urls,start=1):
        payload = {"url": url}
        try:
            r = requests.post(MEALIE_API_URL, headers=headers, json=payload)
        except Exception:
            pass
        upload_progress["index"]=idx
        time.sleep(MEALIE_RATE_LIMIT)
    upload_progress["status"]="done"

# ---------- Progress Endpoints ----------
@app.get("/progress/crawl")
async def crawl_status():
    return crawl_progress

@app.get("/progress/upload")
async def upload_status():
    return upload_progress

# ---------- Download ----------
@app.get("/download")
async def download_file():
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE,"r") as f:
            return PlainTextResponse(f.read())
    return PlainTextResponse("No file found.")

# ---------- Web UI ----------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request":request})
