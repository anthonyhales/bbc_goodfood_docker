import asyncio
import aiohttp
import ssl
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import os
import json
import time
import requests
from fastapi import FastAPI, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import io
import csv
import pandas as pd

# ---------- Config ----------
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
OUTPUT_FILE = os.path.join(DATA_DIR, "bbc_goodfood_mealie_import.txt")
MASTER_RECIPES_FILE = os.path.join(DATA_DIR, "bbc_recipes_master.json")
MEALIE_CONFIG_FILE = os.path.join(DATA_DIR, "mealie_config.json")

BBC_REQUEST_DELAY = 1
MAX_CONCURRENCY = 4
START_URL = "https://www.bbcgoodfood.com/recipes"
USER_AGENT = "GoodFoodMealieCollector/1.0"

# ---------- FastAPI ----------
app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# ---------- Globals ----------
visited_pages = set()
queued_pages = set()
found_recipes_live = []

crawl_progress = {"pages":0, "recipes":0, "status":"idle"}
upload_progress = {"index":0, "total":0, "status":"idle"}
cancel_flag = {"crawl": False, "upload": False}

# ---------- Mealie Config ----------
def load_mealie_config():
    if os.path.exists(MEALIE_CONFIG_FILE):
        with open(MEALIE_CONFIG_FILE) as f:
            return json.load(f)
    return {"url": "", "key": "", "rate_limit": 2}

def save_mealie_config(url, key, rate_limit):
    with open(MEALIE_CONFIG_FILE, "w") as f:
        json.dump({"url": url, "key": key, "rate_limit": rate_limit}, f)

# ---------- Recipe Persistence ----------
def save_recipe(url):
    all_recipes = []
    if os.path.exists(MASTER_RECIPES_FILE):
        with open(MASTER_RECIPES_FILE) as f:
            all_recipes = json.load(f)
    if url not in all_recipes:
        all_recipes.append(url)
        with open(MASTER_RECIPES_FILE, "w") as f:
            json.dump(all_recipes, f)
    with open(OUTPUT_FILE, "a") as f:
        f.write(url+"\n")

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
            if resp.status == 200 and "text/html" in resp.headers.get("Content-Type",""):
                return await resp.text()
    except:
        return None
    return None

def is_true_recipe(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and data.get("@type")=="Recipe":
                return True
            if isinstance(data, list) and any(isinstance(i, dict) and i.get("@type")=="Recipe" for i in data):
                return True
        except:
            continue
    return False

# ---------- Crawl Worker ----------
async def worker(queue, session):
    while not cancel_flag["crawl"]:
        try:
            url = await asyncio.wait_for(queue.get(), timeout=1)
        except asyncio.TimeoutError:
            break
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
            if is_recipe_url(link):
                if link not in found_recipes_live:
                    recipe_html = await fetch(session, link)
                    if recipe_html and is_true_recipe(recipe_html):
                        found_recipes_live.append(link)
                        save_recipe(link)
                        crawl_progress["recipes"] = len(found_recipes_live)
            elif is_internal(link) and link not in visited_pages and link not in queued_pages:
                queued_pages.add(link)
                await queue.put(link)
        queue.task_done()

# ---------- Crawl Start ----------
@app.post("/start_crawl")
async def start_crawl_api():
    cancel_flag["crawl"] = False
    cancel_flag["upload"] = False
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
        for w in workers: w.cancel()
    crawl_progress["status"]="done"
    await push_mealie()

# ---------- Push to Mealie ----------
async def push_mealie():
    upload_progress["status"]="running"
    config = load_mealie_config()
    if not config.get("url") or not config.get("key") or not os.path.exists(OUTPUT_FILE):
        upload_progress["status"]="done"
        return
    headers = {"Authorization": f"Bearer {config['key']}", "Content-Type":"application/json"}
    with open(OUTPUT_FILE) as f:
        urls = [line.strip() for line in f if line.strip()]
    upload_progress["total"] = len(urls)
    for idx, url in enumerate(urls, start=1):
        if cancel_flag["upload"]: break
        try: requests.post(config["url"], headers=headers, json={"url": url})
        except: pass
        upload_progress["index"]=idx
        time.sleep(float(config.get("rate_limit",2)))
    upload_progress["status"]="done"

# ---------- Cancel ----------
@app.get("/cancel")
async def cancel_tasks():
    cancel_flag["crawl"] = True
    cancel_flag["upload"] = True
    return {"status": "cancelled"}

# ---------- Progress Endpoints ----------
@app.get("/progress/crawl")
async def crawl_status(): return crawl_progress
@app.get("/progress/upload")
async def upload_status(): return upload_progress
@app.get("/progress/recipes")
async def live_recipes(): return JSONResponse({"recipes": found_recipes_live})

# ---------- Download ----------
@app.get("/download")
async def download_file():
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE,"r") as f:
            return PlainTextResponse(f.read())
    return PlainTextResponse("No file found.")

# ---------- Recipes Page ----------
@app.get("/recipes", response_class=HTMLResponse)
async def recipes_page(request: Request):
    recipes=[]
    if os.path.exists(MASTER_RECIPES_FILE):
        with open(MASTER_RECIPES_FILE) as f: recipes = json.load(f)
    return templates.TemplateResponse("recipes.html", {"request": request, "recipes": recipes})

@app.get("/recipes/download/{format}")
async def download_recipes(format:str):
    recipes=[]
    if os.path.exists(MASTER_RECIPES_FILE):
        with open(MASTER_RECIPES_FILE) as f: recipes=json.load(f)
    if format=="txt":
        content="\n".join(recipes)
        return StreamingResponse(io.StringIO(content), media_type="text/plain", headers={"Content-Disposition":"attachment; filename=recipes.txt"})
    elif format=="csv":
        output=io.StringIO(); writer=csv.writer(output)
        writer.writerow(["URL"])
        for r in recipes: writer.writerow([r])
        return StreamingResponse(io.StringIO(output.getvalue()), media_type="text/csv", headers={"Content-Disposition":"attachment; filename=recipes.csv"})
    elif format=="excel":
        df=pd.DataFrame(recipes, columns=["URL"])
        output=io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False)
        output.seek(0)
        return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition":"attachment; filename=recipes.xlsx"})

# ---------- Settings Page ----------
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    config=load_mealie_config()
    return templates.TemplateResponse("settings.html", {"request": request, "config": config})

@app.post("/settings/save")
async def settings_save(data: dict = Body(...)):
    save_mealie_config(data["url"], data["key"], data["rate_limit"])
    return {"status": "saved"}

# ---------- Main UI ----------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
