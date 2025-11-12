from http.server import BaseHTTPRequestHandler
import os, json, requests, psycopg2, datetime, time
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup

# ==================================
# 🔧 CONFIGURATION
# ==================================
PINCODES_TO_CHECK = str(os.getenv("PINCODES_TO_CHECK"))
PINCODES_TO_CHECK = PINCODES_TO_CHECK.split(",") 
print(f"[config] Pincodes to check: {PINCODES_TO_CHECK}")
DATABASE_URL = os.getenv("DIRECT_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_GROUP_ID = str(os.getenv("TELEGRAM_GROUP_ID")) # Telegram group ID
CRON_SECRET = os.getenv("CRON_SECRET")

# Flipkart Proxy (AlwaysData)
FLIPKART_PROXY_URL = "https://rknldeals.alwaysdata.net/flipkart_check"

# ==================================
# 🧠 VERCEL HANDLER
# ==================================
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query_components = parse_qs(urlparse(self.path).query)
        auth_key = query_components.get("secret", [None])[0]

        if auth_key != CRON_SECRET:
            self.send_response(401)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode())
            return

        try:
            in_stock_messages, summary = main_logic()

            # ✅ Only send Telegram message if at least one product is available
            if in_stock_messages:
                final_message = (
                    "🔥 *Stock Alert!*\n\n"
                    + "\n\n".join(in_stock_messages)
                    + "\n\n"
                    + summary
                )
                send_telegram_message(final_message)
                print("[info] ✅ Telegram message sent with available products.")
            else:
                print("[info] ❌ No products in stock — skipping Telegram notification.")

            # ✅ Always respond with summary
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"status": "ok", "found": len(in_stock_messages), "summary": summary}
                ).encode()
            )

        except Exception as e:
            print(f"[error] {e}")
            self.send_response(500)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

# ==================================
# 🗄️ DATABASE
# ==================================
def get_products_from_db():
    print("[info] Connecting to database...")
    # NOTE: psycopg2 should be installed if running this locally: pip install psycopg2-binary
    # This line assumes DATABASE_URL is set in environment variables (Vercel/hosting environment).
    conn = psycopg2.connect(DATABASE_URL) 
    cursor = conn.cursor()
    cursor.execute("SELECT name, url, product_id, store_type, affiliate_link FROM products")
    products = cursor.fetchall()
    conn.close()

    products_list = [
        {
            "name": row[0],
            "url": row[1],
            "productId": row[2],
            "storeType": row[3],
            "affiliateLink": row[4],
        }
        for row in products
    ]
    print(f"[info] Loaded {len(products_list)} products from database.")
    return products_list

# ==================================
# 💬 TELEGRAM MESSAGE
# ==================================
def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_GROUP_ID:
        print("[warn] Missing Telegram config.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_GROUP_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print(f"[info] ✅ Message sent to group {TELEGRAM_GROUP_ID}")
        else:
            print(f"[warn] Telegram send failed: {res.text}")
    except Exception as e:
        print(f"[error] Telegram error: {e}")

# ==================================
# 🦄 UNICORN CHECKER (iPhone 17, 256GB)
# ==================================
def check_unicorn():
    """Checks stock for all iPhone 17 (256GB) variants at Unicorn Store."""
    
    # --- API CONFIG ---
    BASE_URL = "https://fe01.beamcommerce.in/get_product_by_option_id"
    HEADERS = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "customer-id": "unicorn",
        "origin": "https://shop.unicornstore.in",
        "referer": "https://shop.unicornstore.in/",
    }
    
    # Fixed product attributes for iPhone 17 (Category 456)
    CATEGORY_ID = "456" 
    FAMILY_ID = "94"
    GROUP_IDS = "57,58"
    STORAGE_256GB_ID = "250" # 256GB Option ID 

    # Color variants to check (ID 57)
    COLOR_VARIANTS = {
        "Lavender": "313",
        "Sage": "311",
        "Mist Blue": "312",
        "White": "314",
        "Black": "315",
    }
    
    available_messages = []
    
    for color_name, color_id in COLOR_VARIANTS.items():
        variant_name = f"iPhone 17 {color_name} 256GB"
        
        payload = {
            "category_id": CATEGORY_ID,
            "family_id": FAMILY_ID,
            "group_ids": GROUP_IDS,
            "option_ids": f"{color_id},{STORAGE_256GB_ID}"
        }

        try:
            res = requests.post(BASE_URL, headers=HEADERS, json=payload, timeout=10)
            res.raise_for_status()
            data = res.json()
            
            product_data = data.get("data", {}).get("product", {})
            quantity = product_data.get("quantity", 0)
            
            # Format price and SKU
            price = f"₹{int(product_data.get('price', 0)):,}" if product_data.get('price') else "N/A"
            sku = product_data.get("sku", "N/A")
            
            # Use the main product page URL for linking
            product_url = "https://shop.unicornstore.in/iphone-17" 
            
            if int(quantity) > 0:
                print(f"[UNICORN] ✅ {variant_name} is IN STOCK ({quantity} units)")
                message = (
                    f"✅ *Unicorn*\n"
                    f"[{variant_name} - {sku}]({product_url})"
                    f"\n💰 Price: {price}, Qty: {quantity}"
                )
                available_messages.append(message)
            else:
                dispatch_note = product_data.get("custom_column_4", "Out of Stock").strip()
                print(f"[UNICORN] ❌ {variant_name} unavailable: {dispatch_note}")
                
        except Exception as e:
            print(f"[error] Unicorn check failed for {variant_name}: {e}")
    
    return available_messages

# ==================================
# 🛒 CROMA CHECKER
# ==================================
def check_croma(product, pincode):
    url = "https://api.croma.com/inventory/oms/v2/tms/details-pwa/"
    payload = {
        "promise": {
            "allocationRuleID": "SYSTEM",
            "checkInventory": "Y",
            "organizationCode": "CROMA",
            "sourcingClassification": "EC",
            "promiseLines": {
                "promiseLine": [
                    {
                        "fulfillmentType": "HDEL",
                        "itemID": product["productId"],
                        "lineId": "1",
                        "requiredQty": "1",
                        "shipToAddress": {"zipCode": pincode},
                        "extn": {"widerStoreFlag": "N"},
                    }
                ]
            },
        }
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "oms-apim-subscription-key": "1131858141634e2abe2efb2b3a2a2a5d",
        "origin": "https://www.croma.com",
        "referer": "https://www.croma.com/",
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        data = res.json()

        lines = (
            data.get("promise", {})
            .get("suggestedOption", {})
            .get("option", {})
            .get("promiseLines", {})
            .get("promiseLine", [])
        )

        if lines:
            print(f"[CROMA] ✅ {product['name']} deliverable to {pincode}")
            return f"✅ *Croma*\n[{product['name']}]({product['affiliateLink'] or product['url']})"

        print(f"[CROMA] ❌ {product['name']} unavailable at {pincode}")
    except Exception as e:
        print(f"[error] Croma check failed for {product['name']}: {e}")
    return None

# ==================================
# 🟣 FLIPKART VIA PROXY
# ==================================
def check_flipkart(product, pincode="132001"):
    """Call Flipkart via AlwaysData proxy."""
    try:
        payload = {"productId": product["productId"], "pincode": pincode}
        res = requests.post(FLIPKART_PROXY_URL, json=payload, timeout=25)

        if res.status_code != 200:
            print(f"[FLIPKART] ⚠️ Proxy failed ({res.status_code}) for {product['name']}")
            return None

        data = res.json()
        response = data.get("RESPONSE", {}).get(product["productId"], {})
        listing = response.get("listingSummary", {})
        available = listing.get("available", False)

        if available:
            price = listing.get("pricing", {}).get("finalPrice", {}).get("decimalValue", None)
            print(f"[FLIPKART] ✅ {product['name']} deliverable to {pincode}")
            return (
                f"✅ *Flipkart*\n[{product['name']}]({product['affiliateLink'] or product['url']})"
                + (f"\n💰 Price: ₹{price}" if price else "")
            )

        print(f"[FLIPKART] ❌ {product['name']} not deliverable at {pincode}")
        return None

    except Exception as e:
        print(f"[error] Flipkart proxy check failed for {product['name']}: {e}")
        return None

# ==================================
# 🧾 AMAZON HTML PARSER CHECKER
# ==================================
def check_amazon(product):
    """Check stock availability by scraping the Amazon product page."""
    url = product["url"]
    print(f"[AMAZON] Checking: {url}")

    headers = {
        "authority": "www.amazon.in",
        "method": "GET",
        "scheme": "https",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "max-age=0",
        "sec-ch-ua": '"Not_A Brand";v="99", "Google Chrome";v="137", "Chromium";v="137"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "upgrade-insecure-requests": "1",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
    }

    try:
        res = requests.get(url, headers=headers, timeout=20)
        print(f"[AMAZON] Status code: {res.status_code}")
        html = res.text
        soup = BeautifulSoup(html, "html.parser")

        title_el = soup.select_one("#productTitle")
        price_el = soup.select_one(".a-price .a-offscreen")
        availability_el = soup.select_one("#availability span")

        title = title_el.get_text(strip=True) if title_el else product["name"]
        price = price_el.get_text(strip=True) if price_el else None
        availability = availability_el.get_text(strip=True).lower() if availability_el else ""

        available_phrases = [
            "in stock",
            "free delivery",
            "delivery by",
            "usually dispatched",
            "get it by",
            "available",
        ]
        available = any(phrase in availability for phrase in available_phrases)

        if available:
            print(f"[AMAZON] ✅ {title} is available at {price}")
            return (
                f"✅ *Amazon*\n"
                f"[{title}]({product['affiliateLink'] or url})"
                + (f"\n💰 {price}" if price else "")
            )
        else:
            print(f"[AMAZON] ❌ {title} appears unavailable.")
            print(f"[debug] Availability text: '{availability}'")
            return None

    except Exception as e:
        print(f"[error] Amazon HTML check failed for {product['name']}: {e}")
        return None

# ==================================
# 🟦 VIVO CHECKER
# ==================================
def check_vivo(product):
    """Check stock availability for Vivo by scraping the product page."""
    url = product["url"]
    print(f"[VIVO] Checking: {url}")
    
    # Use a standard browser user agent
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
    }

    try:
        res = requests.get(url, headers=headers, timeout=20)
        res.raise_for_status()
        html = res.text
        soup = BeautifulSoup(html, "html.parser")

        # Generic stock check indicators
        out_of_stock_indicators = ["Out of stock", "Notify Me", "Sold out", "currently unavailable"]

        # Attempt to find the main action button or stock status
        stock_status_el = soup.select_one(".add-to-cart-btn, #buy-now-button, .product-stock-status")
        
        # Fallback to general page text
        page_text = soup.get_text().lower()

        is_available = True
        
        # Check explicit negative indicators
        if stock_status_el:
            status_text = stock_status_el.get_text(strip=True)
            if any(phrase.lower() in status_text.lower() for phrase in out_of_stock_indicators):
                is_available = False
        elif any(phrase.lower() in page_text for phrase in out_of_stock_indicators):
             is_available = False
        
        # If no explicit negative indicators found, assume availability
        if is_available:
            title_el = soup.select_one("h1.product-title")
            title = title_el.get_text(strip=True) if title_el else product["name"]
            
            print(f"[VIVO] ✅ {title} is available.")
            return (
                f"✅ *Vivo*\n"
                f"[{title}]({product['affiliateLink'] or url})"
            )
        else:
            print(f"[VIVO] ❌ {product['name']} appears unavailable.")
            return None

    except Exception as e:
        print(f"[error] Vivo check failed for {product['name']}: {e}")
        return None

# ==================================
# 🟧 IQOO CHECKER
# ==================================
# Using the same logic as Vivo as a starting point since iQOO is a Vivo sub-brand.
def check_iqoo(product):
    """Check stock availability for iQOO by scraping the product page."""
    # Note: If the iQOO site structure differs significantly, this function may need custom selectors.
    # For now, it delegates to the generic check_vivo function.
    return check_vivo(product)

# ==================================
# 🚀 MAIN LOGIC (MODIFIED to include Unicorn, VIVO, and IQOO)
# ==================================
def main_logic():
    start_time = time.time()
    print("[info] Starting stock check...")
    products = get_products_from_db()
    in_stock = []
    
    # Initialize all counters, including Unicorn, Vivo, and iQOO
    croma_count = flip_count = amazon_count = unicorn_count = vivo_count = iqoo_count = 0
    croma_total = flip_total = amazon_total = unicorn_total = vivo_total = iqoo_total = 0

    # ----------------------------------------------------
    # NEW: Check Unicorn stock separately for iPhone 17 variants
    # ----------------------------------------------------
    unicorn_results = check_unicorn()
    # We checked 5 variants total (all are 256GB)
    unicorn_total = 5 
    unicorn_count = len(unicorn_results)
    if unicorn_results:
        in_stock.extend(unicorn_results)
    
    # ----------------------------------------------------
    # EXISTING: Loop through DB products
    # ----------------------------------------------------
    for product in products:
        result = None
        if product["storeType"] == "croma":
            croma_total += 1
            for pincode in PINCODES_TO_CHECK:
                result = check_croma(product, pincode)
                if result:
                    croma_count += 1
                    in_stock.append(result)
                    break
        elif product["storeType"] == "flipkart":
            flip_total += 1
            for pincode in PINCODES_TO_CHECK:
                result = check_flipkart(product, pincode)
                if result:
                    flip_count += 1
                    in_stock.append(result)
                    break
        elif product["storeType"] == "amazon":
            amazon_total += 1
            result = check_amazon(product)
            if result:
                amazon_count += 1
                in_stock.append(result)
        # --- NEW VIVO CHECK ---
        elif product["storeType"] == "vivo":
            vivo_total += 1
            result = check_vivo(product)
            if result:
                vivo_count += 1
                in_stock.append(result)
        # --- NEW IQOO CHECK ---
        elif product["storeType"] == "iqoo":
            iqoo_total += 1
            result = check_iqoo(product)
            if result:
                iqoo_count += 1
                in_stock.append(result)

    duration = round(time.time() - start_time, 2)
    timestamp = datetime.datetime.now().strftime("%d %b %Y %I:%M %p")

    # Final Summary (Vivo and iQOO lines added)
    summary = (
        f"🟢 *Croma:* {croma_count}/{croma_total}\n"
        f"🟣 *Flipkart:* {flip_count}/{flip_total}\n"
        f"🟡 *Amazon:* {amazon_count}/{amazon_total}\n"
        f"🟦 *Vivo:* {vivo_count}/{vivo_total}\n"
        f"🟧 *iQOO:* {iqoo_count}/{iqoo_total}\n"
        f"🦄 *Unicorn:* {unicorn_count}/{unicorn_total} (256GB)\n"
        f"📦 *Total:* {len(in_stock)} available\n"
        f"🕒 *Checked:* {timestamp}\n"
        f"⏱ *Time taken:* {duration}s"
    )

    print(f"[info] ✅ Found {len(in_stock)} products in stock.")
    print("[info] Summary:\n" + summary)
    return in_stock, summary