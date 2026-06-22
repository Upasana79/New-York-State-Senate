import asyncio
import csv
import os
import sys
import random
import yaml  # type: ignore
import logging
import re
import json
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from typing import Dict, Any

import playwright  # type: ignore
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page, BrowserContext, Locator  # type: ignore
from playwright_stealth import Stealth  # type: ignore

# ---------------- LOAD CONFIG ---------------- 
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config_ao.yaml"
try:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
except FileNotFoundError:
    print(f"Error: Configuration file not found at {_CONFIG_PATH}")
    sys.exit(1)

TARGETS = config.get("targets", {})

# ---------------- BASE PATH ---------------- 
BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"

LOGS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

OUTPUT_FILE = DATA_DIR / config.get("files", {}).get("output_csv", "ao_smartphones.csv")
VISITED_FILE = DATA_DIR / "ao_visited_urls.txt"   
SESSION_DIR = DATA_DIR / "sessions"            
SESSION_DIR.mkdir(exist_ok=True)
LOG_FILE = LOGS_DIR / config.get("files", {}).get("log_file", "ao_scraper.log")

# Setting up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | [%(filename)s:%(lineno)d] | %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

CSV_HEADERS = [
    "Scraping Date", "Retailer", "Country", "Product Category",
    "Product Type", "Brand", "Model Name",
    "Currency", "Price", "Condition", "Storage",
    "Rating", "Review Count", "Stock Status", "URL"
]

GEO_SETTINGS: Dict[str, Dict[str, str]] = config.get("geo_settings", {})
USER_AGENTS = config.get("user_agents", [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
])

semaphore = asyncio.Semaphore(3)
csv_lock = asyncio.Lock()
visited_lock = asyncio.Lock()
visited_urls: set = set()      

# === UTILITIES AND HELPER FUNCTIONS ===

async def safe_close(obj, name="object"):
    try:
        if obj:
            await asyncio.wait_for(obj.close(), timeout=5.0)
    except Exception as e:
        logging.warning(f"Timeout or error closing {name}: {e}")

def get_or_create_session_meta(target_name: str) -> dict:
    meta_file = SESSION_DIR / f"{target_name}_meta.json"
    if meta_file.exists():
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Failed to read session meta for {target_name}: {e}. Creating new.")

    meta = {
        "user_agent": random.choice(USER_AGENTS)
    }
    try:
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        logging.info(f"Generated new session meta (User-Agent) for {target_name}")
    except Exception as e:
        logging.error(f"Failed to save session meta for {target_name}: {e}")
        
    return meta

async def safe_get_text(locator, timeout=3000) -> str | None:
    try:
        await locator.first.wait_for(state="attached", timeout=timeout)
        return (await locator.first.inner_text()).strip()
    except Exception:
        return None

async def human_delay(min_sec: float = 2, max_sec: float = 6):
    base = random.uniform(min_sec, max_sec)
    if random.random() < 0.10:
        base += random.uniform(5, 15) # occassional longer delay
    await asyncio.sleep(base)

def load_visited_urls():
    if VISITED_FILE.exists():
        urls = set(VISITED_FILE.read_text(encoding="utf-8").splitlines())
        visited_urls.update(urls)
        logging.info(f"Checkpoint loaded: {len(urls)} already-visited URLs skipped.")

async def mark_visited(url: str):
    async with visited_lock:
        if url not in visited_urls:
            visited_urls.add(url)
            with open(VISITED_FILE, "a", encoding="utf-8") as f:
                f.write(url + "\n")

async def force_dismiss_cookie_banner(page: Page):
    """
    AO might use OneTrust or simple accept buttons.
    We try a generic approach to click accept buttons.
    """
    try:
        # Generic query for accept buttons
        accept_selectors = [
            'button[id*="accept"]',
            'button[class*="accept"]',
            'button[data-testid*="accept"]',
            '#onetrust-accept-btn-handler',
            '.cb-accept'
        ]
        
        for selector in accept_selectors:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1000):
                await btn.click(force=True)
                await page.wait_for_timeout(500)
                logging.info(f"Accepted cookies via {selector}")
                return
    except Exception:
        pass  

async def goto_with_retry(page: Page, url: str, max_retries: int = 3, timeout: int = 45000):
    for attempt in range(max_retries):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if response is None:
                await asyncio.sleep(5)
                continue

            status = getattr(response, 'status', 200)
            title = await page.title()
            
            if status == 429:
                wait = (2 ** attempt) * 15
                logging.warning(f"429 Too Many Requests — {url}. Backing off {wait}s.")
                await asyncio.sleep(wait)
                continue
            
            is_cloudflare = status in [403, 503] or "just a moment" in title.lower() or "cloudflare" in title.lower() or "verification" in title.lower() or "attention required" in title.lower()
            
            if is_cloudflare:
                logging.warning(f"Cloudflare/Block challenge detected ({status}) — {url}.")
                
                # IMMEDIATE DEBUG DUMP
                try:
                    debug_img = LOGS_DIR / "cf_block_screenshot.png"
                    debug_html = LOGS_DIR / "cf_block_page.html"
                    await page.screenshot(path=str(debug_img))
                    with open(debug_html, "w", encoding="utf-8") as f:
                        f.write(await page.content())
                    logging.info(f"Saved immediate CF debug screenshot to {debug_img.name} and HTML to {debug_html.name}")
                except Exception as e:
                    logging.warning(f"Failed to save CF debug: {e}")

                try:
                    # Move mouse around to trigger Turnstile's human detection
                    for _ in range(4):
                        await page.mouse.move(random.randint(100, 800), random.randint(100, 600))
                        await page.wait_for_timeout(random.randint(200, 500))

                    logging.info("Waiting for Turnstile iframe to spawn...")
                    iframe_elem = await page.wait_for_selector("iframe", state="attached", timeout=15000)
                    
                    if iframe_elem:
                        cf_iframe = page.locator('iframe')
                        count = await cf_iframe.count()
                        logging.info(f"Found {count} iframe(s). Attempting human-like click...")
                        
                        for i in range(count):
                            box = await cf_iframe.nth(i).bounding_box()
                            if box and box["width"] > 0 and box["height"] > 0:
                                cx = box["x"] + 30
                                cy = box["y"] + box["height"] / 2
                                await page.mouse.move(cx, cy, steps=10)
                                await page.wait_for_timeout(400)
                                await page.mouse.click(cx, cy)
                                logging.info(f"Clicked iframe {i} at {cx}, {cy}")
                                break
                        
                        # Wait to see if challenge resolves after click
                        await page.wait_for_timeout(10000)
                except Exception as e:
                    logging.warning(f"Error interacting with CF iframe: {e}")

                # Re-check title
                new_title = await page.title()
                if not ("just a moment" in new_title.lower() or "cloudflare" in new_title.lower() or "verification" in new_title.lower()):
                    logging.info("Successfully bypassed Cloudflare!")
                    return response
                else:
                    wait = 15
                    logging.warning(f"Still blocked by Cloudflare. Cooling down {wait}s before retry.")
                    await asyncio.sleep(wait)
                    continue

            return response

        except PlaywrightTimeoutError:
            wait = (2 ** attempt) * 10
            logging.warning(f"Timeout — {url}. Backing off {wait}s.")
            if attempt < max_retries - 1:
                await asyncio.sleep(wait)

        except Exception as e:
            logging.error(f"Navigation error — {url}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(10)

    logging.error(f"Gave up on {url} after {max_retries} attempts.")
    
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_img = LOGS_DIR / f"ao_timeout_debug_{timestamp}.png"
        debug_html = LOGS_DIR / f"ao_timeout_debug_{timestamp}.html"
        
        await page.screenshot(path=str(debug_img), timeout=10000)
        html_content = await page.content()
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html_content[:2000] + "\n...[TRUNCATED]")
            
        logging.info(f"Saved debug files: {debug_img.name}")
    except Exception as err:
        logging.error(f"Failed to capture debug info: {err}")

    return None

# === MAIN SCRAPING LOGIC ===

async def scrape_target(p, target_name: str, start_url: str, writer):
    async with semaphore:
        # determine region based on the config name
        geo_key = target_name.split("_")[0] if "_" in target_name else "UK"
        geo = GEO_SETTINGS.get(geo_key, GEO_SETTINGS.get("UK", {}))
        
        session_file = SESSION_DIR / f"{target_name}_ao.json"
        meta = get_or_create_session_meta(target_name)

        user_data_dir = SESSION_DIR / f"{target_name}_profile"
        
        stealth_args = [
            '--disable-blink-features=AutomationControlled',
            '--start-maximized'
        ]
        
        try:
            # Using launch_persistent_context exactly like the successful snippet
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                channel="chrome",
                headless=False,
                viewport={"width": 1280, "height": 900},
                args=stealth_args
            )
    
            try:
                await _process_listing_page(context, target_name, start_url, writer, session_file)
            finally:
                await safe_close(context, f"context for {target_name}")
        finally:
            logging.info(f"Browser connection for {target_name} shut down.")

async def _process_listing_page(context: BrowserContext, target_name: str, start_url: str, writer: csv.DictWriter, session_file: Path):
    # launch_persistent_context usually creates a default page at index 0
    pages = context.pages
    page: Page
    if pages:
        page = pages[0]
    else:
        page = await context.new_page()
        
    session_saved = session_file.exists()  
    page_num: int = 1

    try:
        logging.info(f"Navigating to {target_name}: {start_url}")
        
        await asyncio.sleep(random.uniform(1.0, 3.0))
        
        response = await goto_with_retry(page, start_url, max_retries=3, timeout=60000)
        if response is None:
             logging.error(f"Failed to access listing page {start_url}")
             return 
        
        while True:
            logging.info(f"--- Processing page {page_num} ---")
            
            # Dismiss AO's specific cookie banner 
            try:
                accept_btn = page.locator('button:has-text("Accept All"), button:has-text("Accept all")')
                if await accept_btn.count() > 0:
                    await accept_btn.first.click()
                    logging.info("Dismissed AO cookie banner (Accept All)")
                    await page.wait_for_timeout(1000)
                else:
                    decline_btn = page.locator('button:has-text("Decline optional cookies")')
                    if await decline_btn.count() > 0:
                        await decline_btn.first.click()
                        logging.info("Dismissed AO cookie banner (Decline)")
                        await page.wait_for_timeout(1000)
            except Exception:
                pass
            
            # Dismiss email signup modal ("Add a smile to your inbox")
            try:
                maybe_later = page.locator('button:has-text("Maybe later")')
                if await maybe_later.count() > 0:
                    await maybe_later.first.click()
                    logging.info("Dismissed email signup modal (Maybe later)")
                    await page.wait_for_timeout(500)
                else:
                    # Try closing via the X button on the modal
                    close_btn = page.locator('[class*="modal"] button[aria-label="Close"], [class*="popup"] button[aria-label="Close"], [class*="modal"] .close, [class*="popup"] .close')
                    if await close_btn.count() > 0:
                        await close_btn.first.click()
                        logging.info("Dismissed modal via close button")
                        await page.wait_for_timeout(500)
            except Exception:
                pass
            
            if not session_saved:
                session_saved = True
                logging.info(f"Persistent context active for {target_name}.")

            # Wait for JS to fully render product listing
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)  # Extra buffer for JS rendering

            # Scroll to trigger lazy loading of all product cards
            prev_count = 0
            for _ in range(20):
                await page.evaluate("window.scrollBy(0, 800)")  # type: ignore
                await page.wait_for_timeout(500)
                count = await page.evaluate(  # type: ignore
                    "() => { const m = document.querySelector('main') || document.body; "
                    "return m.querySelectorAll('a[href*=\"/product/\"]').length; }"
                )
                if count == prev_count and count > 0:
                    break
                prev_count = count
            await page.evaluate("window.scrollTo(0, 0)")  # type: ignore
            await page.wait_for_timeout(800)

            # Use JavaScript to extract all product data directly from rendered DOM
            products: list[dict[str, str]] = await page.evaluate(r"""() => {
                const results = [];
                const main = document.querySelector('main') || document.body;
                const seen = new Set();

                const allLinks = main.querySelectorAll('a[href*="/product/"]');

                for (const link of allLinks) {
                    const rawHref = link.getAttribute('href') || '';
                    // Strip hash fragment (#reviewsSection etc.) to deduplicate
                    const href = rawHref.split('#')[0];
                    if (!href || seen.has(href)) continue;

                    // Walk up from link until we find a container with a price element
                    let card = link;
                    for (let i = 0; i < 10; i++) {
                        if (card.querySelector && card.querySelector('[data-testid="price-now"]')) break;
                        if (!card.parentElement) break;
                        card = card.parentElement;
                    }

                    // Name — prefer itemprop="name" h2
                    const nameEl = card.querySelector('h2[itemprop="name"], h3[itemprop="name"], h2, h3');
                    const name = (nameEl ? nameEl.textContent : link.textContent || '').trim();
                    if (!name || name.length < 5 || name.length > 300) continue;

                    // Price
                    const priceEl = card.querySelector('[data-testid="price-now"]');
                    const priceRaw = priceEl ? priceEl.textContent.trim() : '';
                    if (!priceRaw) continue;

                    // Rating — look for "X / 5" or "X out of 5" in aria-labels or text
                    let rating = 'N/A';
                    const ratingEls = card.querySelectorAll(
                        '[aria-label*="out of"], [aria-label*="star"], [aria-label*="Star"], ' +
                        '[aria-label*="rating"], [aria-label*="Rating"], ' +
                        '[class*="rating"], [class*="Rating"], ' +
                        '[data-rating], [itemprop="ratingValue"]'
                    );
                    for (const el of ratingEls) {
                        const label = el.getAttribute('aria-label') || '';
                        const val = el.getAttribute('data-rating') || el.getAttribute('content') || '';
                        const text = el.textContent.trim();
                        const m = (label + ' ' + val + ' ' + text).match(/(\d+(?:\.\d+)?)\s*(?:out of|\/|\s*stars?)/i);
                        if (m) { rating = m[1]; break; }
                        if (/^\d(\.\d+)?$/.test(text) && parseFloat(text) <= 5) { rating = text; break; }
                        if (/\d/.test(label)) { rating = label; break; }
                    }

                    // Review count — look for "(NNN Reviews)" or "NNN reviews" pattern
                    let reviewCount = 'N/A';
                    const reviewEls = card.querySelectorAll(
                        '[class*="review"], [class*="Review"], ' +
                        '[itemprop="reviewCount"], [itemprop="ratingCount"]'
                    );
                    for (const el of reviewEls) {
                        const text = el.textContent.trim();
                        const m = text.match(/(\d[\d,]*)\s*(?:review|rating|customer)/i) || text.match(/^\((\d[\d,]*)\)$/);
                        if (m) { reviewCount = m[1]; break; }
                    }
                    if (reviewCount === 'N/A') {
                        const cardText = card.textContent || '';
                        const m = cardText.match(/\((\d[\d,]+)\s*Reviews?\)/i);
                        if (m) reviewCount = m[1];
                    }

                    // Stock status — add-to-basket button = In Stock; out-of-stock class = Out of Stock
                    let stockStatus = 'N/A';
                    const outEl = card.querySelector(
                        '[class*="out-of-stock"], [class*="unavailable"], [class*="sold-out"], ' +
                        '[class*="OutOfStock"], [class*="Unavailable"]'
                    );
                    const addBtn = card.querySelector(
                        'button[class*="add"], button[class*="basket"], ' +
                        'button[aria-label*="basket"], button[aria-label*="Add to basket"]'
                    );
                    const cardText = (card.textContent || '').toLowerCase();
                    if (outEl) stockStatus = 'Out of Stock';
                    else if (addBtn) stockStatus = 'In Stock';
                    else if (/out.of.stock|unavailable|sold.out/.test(cardText)) stockStatus = 'Out of Stock';
                    else if (/add.to.basket|in.stock/.test(cardText)) stockStatus = 'In Stock';

                    seen.add(href);
                    results.push({
                        name: name,
                        price: priceRaw,
                        href: href,
                        rating: rating,
                        reviewCount: reviewCount,
                        stockStatus: stockStatus,
                    });
                }

                return results;
            }""")

            if not products:
                logging.warning(f"No products extracted from page {page_num}. Checking if page is blocked...")
                # Debug dump
                debug_img = LOGS_DIR / f"ao_page{page_num}_debug.png"
                debug_html = LOGS_DIR / f"ao_page{page_num}_debug.html"
                await page.screenshot(path=str(debug_img))
                with open(debug_html, "w", encoding="utf-8") as f:
                    f.write(await page.content())
                logging.info(f"Saved debug files for page {page_num}")
                break

            logging.info(f"Extracted {len(products)} products from page {page_num}.")

            for product in products:
                name = product.get("name", "N/A")
                price_raw = product.get("price", "N/A")
                href = product.get("href", "")
                rating = product.get("rating", "N/A")
                review_count = product.get("reviewCount", "N/A")
                stock_status = product.get("stockStatus", "N/A")

                # Filter out junk entries (out-of-stock placeholders, nav links)
                if not name or "back in stock" in name.lower() or "let me know" in name.lower():
                    continue

                full_url = f"https://ao.com{href}" if href.startswith('/') else href

                # Parse price — extract only digits and one decimal point
                price_match = re.search(r'[\d,]+\.?\d{0,2}', price_raw)
                price_val = price_match.group(0).replace(',', '') if price_match else 'N/A'

                # Parse storage from name
                storage = "N/A"
                if isinstance(name, str):
                    storage_match = re.search(r'(\d+)\s*(GB|TB|MB)', name, re.IGNORECASE)
                    if storage_match:
                        storage = f"{storage_match.group(1)}{storage_match.group(2).upper()}"

                async with csv_lock:
                    writer.writerow({
                        "Scraping Date": datetime.today().strftime("%m/%d/%Y"),
                        "Retailer": "AO",
                        "Country": target_name.split("_")[0] if "_" in target_name else "UK",
                        "Product Category": "Smart Phone",
                        "Product Type": "New",
                        "Brand": "Unknown",
                        "Model Name": name,
                        "Price": price_val,
                        "Currency": "£",
                        "Condition": "New",
                        "Storage": storage,
                        "Rating": rating,
                        "Review Count": review_count,
                        "Stock Status": stock_status,
                        "URL": full_url
                    })

                logging.info(f"   [ROW] {name} | £{price_val} | {storage} | ★{rating} ({review_count} reviews) | {stock_status}")

            await mark_visited(page.url)  # type: ignore
            page_num += 1  # type: ignore

            # Pagination: Look for next page button/link
            next_selectors = [
                'a[aria-label*="Next"]',
                'a[aria-label*="next"]', 
                'button[aria-label*="Next"]',
                'a:has-text("Next")',
                'a.pagination__next',
                '[class*="pagination"] a:last-child',
                'nav a:last-child'
            ]
            
            # Dismiss any popup overlays that might block clicks
            try:
                await page.evaluate("""() => {  # type: ignore
                    // Remove the marketing popup
                    const popup = document.querySelector('#wps_popup');
                    if (popup) popup.remove();
                    // Remove email signup modals
                    document.querySelectorAll('[class*="modal"], [class*="popup"], [class*="overlay"]').forEach(el => {
                        const style = getComputedStyle(el);
                        if (style.position === 'fixed' || style.position === 'absolute') {
                            if (style.zIndex > 100 || el.id.includes('popup') || el.id.includes('modal')) {
                                el.remove();
                            }
                        }
                    });
                }""")
            except Exception:
                pass

            next_found = False
            for sel in next_selectors:
                next_button = page.locator(sel)
                if await next_button.count() > 0:
                    try:
                        await next_button.first.click(force=True)
                        logging.info(f"Clicked next page button ({sel}).")
                        try:
                            await page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        await human_delay(3, 7)
                        next_found = True
                        break
                    except Exception as e:
                        logging.warning(f"Failed clicking next with {sel}: {e}")
            
            if not next_found:
                logging.info(f"No next page button found after page {page_num - 1}. Done.")
                break

    except Exception as e:
        logging.error(f"Error scraping listing {target_name}: {e}")
    finally:
        await safe_close(page, f"page for {target_name}")

async def _scrape_product_page(context: BrowserContext, url: str, target_name: str, writer: csv.DictWriter):
    page: Page = await context.new_page()
    await Stealth().apply_stealth_async(page)

    try:
        res = await goto_with_retry(page, url)
        if not res: return
        
        await force_dismiss_cookie_banner(page)
        await asyncio.sleep(2) # hydration buffer

        # Extract Name
        model_name = "N/A"
        title_elem = page.locator('h1').first
        if await title_elem.count() > 0:
            text_val = await safe_get_text(title_elem)
            if text_val:
                model_name = text_val

        # Extract Price (AO.com specific price locations)
        price = "N/A"
        currency = "£"
        
        price_selectors = [
            '[data-testid="price"]', 
            '.price', 
            '[itemprop="price"]',
            '.product-price'
        ]
        
        for selector in price_selectors:
            price_elem = page.locator(selector).first
            if await price_elem.count() > 0:
                raw_price = await safe_get_text(price_elem)
                if raw_price:
                    match = re.search(r'[\d.,]+', raw_price)
                    if match:
                        price = match.group(0)
                        break
        
        # Extract Storage if available (sometimes in title or variant selectors)
        storage = "N/A"
        if isinstance(model_name, str):
            storage_match = re.search(r'(\d+)\s*(GB|TB|MB)', model_name, re.IGNORECASE)
            if storage_match:
                storage = f"{storage_match.group(1)}{storage_match.group(2).upper()}"

        condition = "New" # AO mainly sells new

        async with csv_lock:
            writer.writerow({
                "Scraping Date": datetime.today().strftime("%m/%d/%Y"),
                "Retailer": "AO",
                "Country": target_name.split("_")[0] if "_" in target_name else "UK",
                "Product Category": "Smart Phone",
                "Product Type": "New",
                "Brand": "Unknown",
                "Model Name": model_name,
                "Price": price,
                "Currency": currency,
                "Condition": condition,
                "Storage": storage,
                "Rating": "N/A",
                "Review Count": "N/A",
                "Stock Status": "N/A",
                "URL": page.url
            })
            
        logging.info(f"   [EXTRACTED] {model_name} | {currency}{price} | {storage}")
        
        await mark_visited(url)

    except Exception as e:
        logging.warning(f"Failed to extract from {url}: {e}")
    finally:
        await safe_close(page, f"product page {url}")

async def main():
    load_visited_urls()
    
    file_exists = OUTPUT_FILE.exists()
    
    with open(OUTPUT_FILE, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not file_exists:
            writer.writeheader()
            
        async with async_playwright() as p:
            tasks = []
            for target_name, url in TARGETS.items():
                if url:
                    tasks.append(scrape_target(p, target_name, url, writer))
                    
            if tasks:
                await asyncio.gather(*tasks)

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Script manually interrupted.")
