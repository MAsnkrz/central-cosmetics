"""
Central Cosmetics Monitor
Monitors https://centralcosmetics.co.uk/new-arrivals/

Uses:
  - WooCommerce Store API for full baseline snapshot
  - HTML scraping of /new-arrivals/ for incremental new product detection
  - Product page scraping for stock count

Detects (Discord alerts fire ONLY for these):
  - New product listings (in stock only)
  - Price drops (decreased >1% and >£0.02)
  - Restocks (stock increased meaningfully) / Back in stock

Does NOT alert on: price increases, stock decreases, going OOS.

Deps: pip install requests beautifulsoup4
"""

import json
import os
import re
import time
import random
import requests
from datetime import datetime, timezone
from urllib.parse import quote
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL        = "https://centralcosmetics.co.uk"
NEW_ARRIVALS    = f"{BASE_URL}/new-arrivals/"
API_URL         = f"{BASE_URL}/wp-json/wc/store/v1/products"
SNAPSHOT_FILE   = "snapshot_central.json"
REQUEST_DELAY   = 2.0
RUN_ONCE        = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", "1800"))  # 30 min

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Discord colours
COLOUR_NEW     = 0xE91E8C   # pink — new listing
COLOUR_RESTOCK = 0x3498DB   # blue — restock
COLOUR_BACK    = 0x9B59B6   # purple — back in stock
# Price drop colours are tiered by severity — see notify_price_change()

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def get_soup(url, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=15)
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"  [!] Rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"  [!] Fetch error ({url}): {e} — attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(4 * (attempt + 1))
    return None


def api_get(page, per_page=100, orderby="date", order="desc", retries=3):
    params = {"page": page, "per_page": per_page, "orderby": orderby, "order": order}
    for attempt in range(retries):
        try:
            r = SESSION.get(API_URL, params=params, timeout=20)
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"  [!] API rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            total_pages = int(r.headers.get("X-WP-TotalPages", 1))
            total_items = int(r.headers.get("X-WP-Total", 0))
            return r.json(), total_pages, total_items
        except Exception as e:
            print(f"  [!] API error (page {page}): {e} — attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    return [], 0, 0

# ---------------------------------------------------------------------------
# SCRAPING — NEW ARRIVALS PAGE
# ---------------------------------------------------------------------------

def scrape_new_arrivals_page():
    """
    Scrape /new-arrivals/ listing. Returns list of product dicts.
    Each product has: id, slug, title, url, price, barcode, brand, image.
    """
    soup = get_soup(NEW_ARRIVALS)
    if not soup:
        return []

    products = []
    seen = set()

    for a in soup.find_all("a", href=re.compile(r"/product/[^/?#]+")):
        href = a["href"]
        if any(x in href for x in ["add-to-cart", "wishlist", "?", "#"]):
            continue
        m = re.search(r"/product/([^/?#]+)/?", href)
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)

        card = a.find_parent("div") or a.find_parent("li")
        title = barcode = price = brand = image = ""

        if card:
            title_el = card.find(["h3", "h2", "h4"])
            if title_el:
                title = title_el.get_text(strip=True)

            ct = card.get_text(" ", strip=True)

            # EAN from listing
            ean_m = re.search(r"EAN:\s*([0-9]{8,14})", ct)
            if ean_m:
                barcode = ean_m.group(1)

            # Price ex VAT — "£2.50 +VAT"
            price_m = re.search(r"£([\d.]+)\s*\+VAT", ct)
            if price_m:
                price = price_m.group(1)

            # Brand link
            brand_a = card.find("a", href=re.compile(r"/brand/"))
            if brand_a:
                brand = brand_a.get_text(strip=True)

            # Image — lazy loaded, check data-src or src
            img = card.find("img")
            if img:
                image = img.get("data-src") or img.get("src") or ""
                # If it's a lazy placeholder, build from EAN
                if "lazy.svg" in image and barcode:
                    image = f"{BASE_URL}/wp-content/uploads/{barcode}___L.jpg"

        products.append({
            "slug":      slug,
            "title":     title,
            "url":       f"{BASE_URL}/product/{slug}/",
            "price":     price,
            "barcode":   barcode,
            "brand":     brand,
            "image":     image,
        })

    return products

# ---------------------------------------------------------------------------
# SCRAPING — PRODUCT DETAIL PAGE
# ---------------------------------------------------------------------------

def scrape_product_page(slug, existing=None):
    """Scrape individual product page for full details including stock."""
    url = f"{BASE_URL}/product/{slug}/"
    soup = get_soup(url)
    if not soup:
        return existing or {"slug": slug, "url": url}

    product = (existing or {}).copy()
    product["slug"] = slug
    product["url"]  = url

    text    = soup.get_text(" ", strip=True)
    rel_idx = text.find("Related products")
    section = text[:rel_idx] if rel_idx > 0 else text

    # Title
    h1 = soup.find("h1")
    if h1:
        product["title"] = h1.get_text(strip=True)

    # Image from og:image (reliable, always present)
    og_img = soup.find("meta", property="og:image")
    if og_img:
        product["image"] = og_img.get("content", "")

    # EAN
    ean_m = re.search(r"EAN:\s*([0-9]{8,14})", section)
    if ean_m:
        product["barcode"] = ean_m.group(1)
    elif not product.get("barcode"):
        ean_m2 = re.search(r"\b([0-9]{13})\b", section)
        product["barcode"] = ean_m2.group(1) if ean_m2 else ""

    # SKU
    sku_m = re.search(r"SKU[:\s]+([A-Z0-9\-]+)", section)
    product["sku"] = sku_m.group(1) if sku_m else ""

    # Price ex VAT
    price_m = re.search(r"£([\d.]+)\s*\+VAT", section)
    if price_m:
        product["price"] = price_m.group(1)

    # Sale price (if crossed out original)
    price_box = soup.find("p", class_=re.compile(r"price"))
    if price_box:
        del_tag = price_box.find("del")
        ins_tag = price_box.find("ins")
        if del_tag and ins_tag:
            orig = re.search(r"£([\d.]+)", del_tag.get_text())
            sale = re.search(r"£([\d.]+)", ins_tag.get_text())
            if orig: product["original_price"] = orig.group(1)
            if sale: product["price"] = sale.group(1)

    # Brand
    brand_a = soup.find("a", href=re.compile(r"/brand/"))
    if brand_a:
        product["brand"] = brand_a.get_text(strip=True)

    # Stock
    stock_m = re.search(r"(\d+)\s+in\s+stock", section)
    if stock_m:
        product["stock"]    = int(stock_m.group(1))
        product["in_stock"] = True
    elif "out of stock" in section.lower():
        product["stock"]    = 0
        product["in_stock"] = False
    else:
        product.setdefault("stock",    None)
        product.setdefault("in_stock", True)

    return product

# ---------------------------------------------------------------------------
# API BASELINE
# ---------------------------------------------------------------------------

def parse_api_product(item):
    """Parse a WooCommerce Store API product into our format."""
    def pence_to_pounds(val):
        try:
            return f"{int(val) / 100:.2f}"
        except (TypeError, ValueError):
            return ""

    prices    = item.get("prices", {})
    price     = pence_to_pounds(prices.get("price", ""))
    regular   = pence_to_pounds(prices.get("regular_price", ""))
    sale_price = price if (regular and price != regular) else ""
    base_price = regular if regular else price

    name     = item.get("name", "")
    desc     = (item.get("description", "") or "") + " " + (item.get("short_description", "") or "")
    ean_m    = re.search(r"EAN[:\s]*([0-9]{8,14})", desc)
    barcode  = ean_m.group(1) if ean_m else ""

    images   = item.get("images", [])
    image    = images[0].get("src", "") if images else ""

    stock_status = item.get("stock_status", "instock")
    stock_qty    = item.get("stock_quantity")
    # Prefer real quantity for in_stock determination; fall back to status text
    if stock_qty is not None:
        in_stock = stock_qty > 0
    else:
        in_stock = stock_status in ("instock", "onbackorder")

    return {
        "id":             str(item.get("id", "")),
        "slug":           item.get("slug", ""),
        "title":          name,
        "url":            item.get("permalink", f"{BASE_URL}/product/{item.get('slug', '')}/"),
        "image":          image,
        "barcode":        barcode,
        "sku":            item.get("sku", ""),
        "brand":          "",
        "price":          base_price,
        "original_price": base_price if sale_price else "",
        "stock":          stock_qty,
        "in_stock":       in_stock,
    }


def fetch_all_via_api():
    """Fetch all products via WooCommerce Store API."""
    print("  Fetching page 1 from API...")
    items, total_pages, total_items = api_get(1)
    if not items:
        return []

    print(f"  {total_items} products across {total_pages} pages")
    all_products = [parse_api_product(i) for i in items]

    for page in range(2, total_pages + 1):
        time.sleep(REQUEST_DELAY + random.uniform(0, 1))
        print(f"  Fetching page {page}/{total_pages}...")
        items, _, _ = api_get(page)
        all_products.extend([parse_api_product(i) for i in items])

    return all_products

# ---------------------------------------------------------------------------
# PRICING / DISCORD HELPERS
# ---------------------------------------------------------------------------

def vat_price(price_str):
    try:
        return f"{float(price_str) * 1.2:.2f}"
    except (ValueError, TypeError):
        return price_str


def selleramp_url(barcode, cost_price_str):
    if not barcode:
        return None
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode}&sas_cost_price={vat_price(cost_price_str)}"
    )


def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _base_fields(product):
    barcode = product.get("barcode", "")
    sku     = product.get("sku", "")
    brand   = product.get("brand", "")
    stock   = product.get("stock")
    in_stock = product.get("in_stock", True)
    price   = product.get("price", "")
    sas_url = selleramp_url(barcode, price)

    if stock is not None:
        stock_val = f"**{stock} units**"
    elif in_stock:
        stock_val = "✅ In stock"
    else:
        stock_val = "❌ Out of stock"

    fields = [
        {"name": "🏷️ Brand",        "value": brand if brand else "-",                    "inline": True},
        {"name": "🔢 Barcode / EAN", "value": f"`{barcode}`" if barcode else "-",         "inline": True},
        {"name": "🔖 SKU",           "value": f"`{sku}`" if sku else "-",                 "inline": True},
        {"name": "📊 Stock",         "value": stock_val,                                  "inline": True},
    ]
    if sas_url:
        fields.append({"name": "🔍 SellerAmp SAS", "value": f"[Open in SellerAmp]({sas_url})", "inline": False})
    return fields


def _send_embed(embed):
    payload = {"embeds": [embed]}
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def _thumbnail(product):
    image = product.get("image", "")
    return {"url": image} if image else None


def notify_new(product):
    price = product.get("price", "")
    orig  = product.get("original_price", "")

    if orig and orig != price:
        price_display = f"£{orig} +VAT -> **£{price} +VAT**"
    else:
        price_display = f"**£{price} +VAT**" if price else "-"

    fields = [
        {"name": "💰 Price (ex. VAT)",  "value": price_display,                    "inline": True},
        {"name": "💷 Price (inc. VAT)", "value": f"£{vat_price(price)}" if price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🆕  NEW LISTING — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_NEW,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Central Cosmetics Monitor • centralcosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: NEW — {product.get('title', '')[:60]}")


def notify_price_change(product, old_price, new_price, pct_change):
    """
    pct_change is a fraction (e.g. 0.05 = 5% drop). Always a drop —
    price increases are no longer tracked.
    Colour tier scales with drop severity for quick visual triage.
    """
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct_display = f"{pct_change * 100:.1f}%"

    if pct_change >= 0.20:
        colour = 0x00C853   # deep green — big drop (20%+)
        tier   = "🔥"
    elif pct_change >= 0.10:
        colour = 0x2ECC71   # green — solid drop (10-20%)
        tier   = "💰"
    else:
        colour = 0x82E0AA   # light green — small drop (1-10%)
        tier   = "💵"

    fields = [
        {"name": "💰 Old Price (ex. VAT)", "value": f"£{old_price} +VAT",                          "inline": True},
        {"name": "💰 New Price (ex. VAT)", "value": f"**£{new_price} +VAT**",                       "inline": True},
        {"name": "📉 Drop",                "value": f"↓ {diff} (**{pct_display}**)",               "inline": True},
        {"name": "💷 New Price (inc. VAT)","value": f"£{vat_price(new_price)}",                     "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{tier}  PRICE DROP -{pct_display} — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Central Cosmetics Monitor • centralcosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: PRICE DROP -{pct_display} — {product.get('title', '')[:50]}")


def notify_stock_change(product, old_stock, new_stock):
    """Restock only — stock decreases are no longer tracked."""
    diff = (new_stock - old_stock) if (new_stock is not None and old_stock is not None) else "?"
    fields = [
        {"name": "📊 Old Stock", "value": f"{old_stock} units",     "inline": True},
        {"name": "📊 New Stock", "value": f"**{new_stock} units**", "inline": True},
        {"name": "📈 Change",    "value": f"↑ +{diff} units" if isinstance(diff, int) else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  RESTOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_RESTOCK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Central Cosmetics Monitor • centralcosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: RESTOCK — {product.get('title', '')[:50]}")


def notify_back_in_stock(product):
    price = product.get("price", "")
    fields = [
        {"name": "💰 Price (ex. VAT)",  "value": f"£{price} +VAT" if price else "-", "inline": True},
        {"name": "💷 Price (inc. VAT)", "value": f"£{vat_price(price)}" if price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  BACK IN STOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_BACK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Central Cosmetics Monitor • centralcosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: BACK IN STOCK — {product.get('title', '')[:60]}")

# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"  [!] Snapshot file is corrupted ({e}) — backing it up and starting fresh.")
            try:
                backup_name = f"{SNAPSHOT_FILE}.corrupted.{int(time.time())}"
                os.rename(SNAPSHOT_FILE, backup_name)
                print(f"  [!] Corrupted file saved as {backup_name}")
            except OSError as backup_err:
                print(f"  [!] Could not back up corrupted file: {backup_err}")
            return {}
    return {}


def save_snapshot(data):
    """Write atomically — write to a temp file then rename, so a crash
    mid-write never leaves a corrupted snapshot.json behind."""
    tmp_file = f"{SNAPSHOT_FILE}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_file, SNAPSHOT_FILE)


def snapshot_entry(product):
    return {
        "title":          product.get("title", ""),
        "url":            product.get("url", ""),
        "image":          product.get("image", ""),
        "barcode":        product.get("barcode", ""),
        "sku":            product.get("sku", ""),
        "brand":          product.get("brand", ""),
        "price":          product.get("price", ""),
        "original_price": product.get("original_price", ""),
        "stock":          product.get("stock"),
        "in_stock":       product.get("in_stock", True),
        "first_seen":     product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated":   datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# CHANGE DETECTION
# ---------------------------------------------------------------------------

def check_changes(product, old):
    """
    Only fires alerts for:
      - Back in stock (was OOS, now has stock) — takes priority
      - Restock (stock increased meaningfully while already in stock)
      - Price drop (decreased by more than 1% AND more than £0.02)
    No alerts for: price increases, stock decreases, going OOS.
    """
    old_price    = old.get("price", "")
    new_price    = product.get("price", "")
    old_stock    = old.get("stock")
    new_stock    = product.get("stock")
    was_in_stock = old.get("in_stock", True)
    now_in_stock = product.get("in_stock", True)

    for key in ("image", "barcode", "sku", "brand"):
        if not product.get(key):
            product[key] = old.get(key, "")

    old_f = safe_float(old_price)
    new_f = safe_float(new_price)

    # Back in stock takes priority over everything else
    if not was_in_stock and now_in_stock:
        notify_back_in_stock(product)
        time.sleep(1)
        return

    # Price drop — require both a meaningful % AND absolute change
    if old_f and new_f and old_f > 0:
        pct_change = (old_f - new_f) / old_f
        abs_change = old_f - new_f
        if pct_change > 0.01 and abs_change > 0.02:
            notify_price_change(product, old_price, new_price, pct_change)
            time.sleep(1)

    # Restock — only while staying in stock, with a sane threshold to avoid noise
    if old_stock is not None and new_stock is not None and was_in_stock and now_in_stock:
        threshold = max(5, int(old_stock * 0.2))
        if new_stock > old_stock + threshold:
            notify_stock_change(product, old_stock, new_stock)
            time.sleep(1)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_check():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Checking Central Cosmetics...")

    snapshot     = load_snapshot()
    known_slugs  = set(snapshot.keys())
    is_first_run = len(known_slugs) == 0

    if is_first_run:
        # ----------------------------------------------------------------
        # FIRST RUN: full store baseline via API — no Discord alerts
        # ----------------------------------------------------------------
        print("  First run — building full store baseline (no alerts)...")

        # Try API first
        all_products = fetch_all_via_api()

        # If API blocked, fall back to scraping new arrivals page only
        if not all_products:
            print("  API unavailable — falling back to new arrivals page scrape...")
            all_products = scrape_new_arrivals_page()

        print(f"  {len(all_products)} products fetched for baseline")

        for i, product in enumerate(all_products, 1):
            slug = product.get("slug") or product.get("id", str(i))
            # Enrich with product page detail (stock, full price, image)
            time.sleep(REQUEST_DELAY + random.uniform(0, 1))
            enriched = scrape_product_page(slug, existing=product)
            entry = snapshot_entry(enriched)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[slug] = entry

            if i % 50 == 0:
                save_snapshot(snapshot)
                print(f"  Auto-saved at {i}/{len(all_products)}")

        save_snapshot(snapshot)
        print(f"  Baseline complete — {len(snapshot)} products. No alerts sent.")

    else:
        # ----------------------------------------------------------------
        # SUBSEQUENT RUNS: scrape new arrivals + check for changes
        # ----------------------------------------------------------------

        # 1. Check new arrivals page for new products
        print("  Scraping new arrivals page...")
        new_arrivals = scrape_new_arrivals_page()
        new_slugs    = [p["slug"] for p in new_arrivals if p["slug"] not in known_slugs]
        print(f"  {len(new_arrivals)} on new arrivals page, {len(new_slugs)} new")

        for slug in new_slugs:
            existing = next((p for p in new_arrivals if p["slug"] == slug), {})
            time.sleep(REQUEST_DELAY + random.uniform(0, 1))
            product = scrape_product_page(slug, existing=existing)

            # Skip Discord alert if product is out of stock — still record it
            if product.get("in_stock", True) and (product.get("stock") is None or product.get("stock") > 0):
                notify_new(product)
                time.sleep(1.5)

            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[slug] = entry

        # 2. Re-check all known products via API for price/stock changes
        print(f"  Re-fetching full store via API to check for changes...")
        all_current = fetch_all_via_api()

        if all_current:
            for product in all_current:
                slug = product.get("slug") or product.get("id", "")
                if slug not in snapshot:
                    continue  # already handled above as new arrival

                old = snapshot[slug]
                # Enrich with page scrape for accurate stock
                time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))
                enriched = scrape_product_page(slug, existing=product)
                check_changes(enriched, old)
                entry = snapshot_entry(enriched)
                entry["first_seen"] = old.get("first_seen", entry["first_seen"])
                snapshot[slug] = entry

        save_snapshot(snapshot)
        print(f"  Snapshot saved ({len(snapshot)} products tracked)")


def main():
    print("=" * 55)
    print("  Central Cosmetics Monitor")
    print(f"  Watching: {NEW_ARRIVALS}")
    print("  Tracking: new listings, price & stock changes")
    print("=" * 55)

    if RUN_ONCE:
        run_check()
    else:
        while True:
            try:
                run_check()
            except Exception as e:
                print(f"  [!] Unexpected error: {e}")
            print(f"  Sleeping {CHECK_INTERVAL}s...")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
