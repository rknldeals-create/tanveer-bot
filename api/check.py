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
# DATABASE_URL: Used for psycopg2 connection
DATABASE_URL = os.getenv("DIRECT_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_GROUP_ID = str(os.getenv("TELEGRAM_GROUP_ID")) # Telegram group ID
CRON_SECRET = os.getenv("CRON_SECRET")

# --- LICENSING CONFIG ---
# NOTE: These must be set as environment variables (e.g., Vercel)
LICENSE_SERVER_URL = os.getenv("LICENSE_SERVER_URL")
CLIENT_ID = os.getenv("CLIENT_ID")
LICENSE_KEY = os.getenv("LICENSE_KEY")

# --- CACHING LOGIC CONFIG ---
# If the local license is valid for less than this time (in days), try to refresh
MIN_REFRESH_INTERVAL_DAYS = 3 
# The duration (in months) to extend the local license if the server says it's OK.
LICENSE_EXTENSION_MONTHS = 1

# Flipkart Proxy (AlwaysData)
FLIPKART_PROXY_URL = "https://rknldeals.alwaysdata.net/flipkart_check"

# ==================================
# 🗄️ DATABASE HELPERS
# ==================================

def get_db_connection():
    """Returns a new psycopg2 connection configured for DIRECT_URL."""
    if not DATABASE_URL:
        raise Exception("DATABASE_URL environment variable is not set.")
    # Set timezone awareness for datetime objects
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor().execute("SET timezone TO 'UTC'")
    return conn

def get_license_info():
    """Retrieves the local license validity date from the database."""
    print("[info] LICENSING: Fetching local license info...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Query the 'licenses' table (mapped from ClientLicense model)
        cursor.execute(
            "SELECT valid_until FROM licenses WHERE client_id = %s",
            (CLIENT_ID,)
        )
        result = cursor.fetchone()
        conn.close()
    except Exception as e:
        print(f"[error] LICENSING: Database read error (Did you run the Prisma migration?): {e}")
        return None
    
    if result:
        # returns the datetime object, implicitly UTC due to SET timezone above
        valid_until = result[0].replace(tzinfo=datetime.timezone.utc)
        print(f"[info] LICENSING: Found local license valid until {valid_until.isoformat()}")
        return valid_until
    
    print("[info] LICENSING: No existing local license found in DB.")
    return None

def update_license_info(new_valid_until):
    """Updates or inserts the local license validity date."""
    # Ensure datetime object has UTC timezone info
    if new_valid_until.tzinfo is None:
         new_valid_until = new_valid_until.replace(tzinfo=datetime.timezone.utc)
         
    print(f"[info] LICENSING: Updating local license to {new_valid_until.isoformat()}...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Use UPSERT logic (INSERT ON CONFLICT UPDATE) for PostgreSQL
        # This inserts if the client_id is new, or updates if it already exists.
        cursor.execute(
            """
            INSERT INTO licenses (client_id, license_key, valid_until) 
            VALUES (%s, %s, %s)
            ON CONFLICT (client_id) 
            DO UPDATE SET valid_until = EXCLUDED.valid_until, license_key = EXCLUDED.license_key
            """,
            (CLIENT_ID, LICENSE_KEY, new_valid_until)
        )
        conn.commit()
        conn.close()
        print("[info] ✅ LICENSING: Local license successfully updated/extended.")
    except Exception as e:
        print(f"[error] LICENSING: Failed to update local license: {e}")

# ==================================
# 🔑 LICENSE CHECKER (DB CACHE + REMOTE REFRESH)
# ==================================
def check_license():
    """
    Checks the local DB cache first. If near expiry, calls the remote server 
    to validate and extend the local license.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    
    # 1. Input/Config Check
    if not all([LICENSE_SERVER_URL, CLIENT_ID, LICENSE_KEY]):
        print("[error] ❌ LICENSING: Missing required environment variables (LICENSE_SERVER_URL, CLIENT_ID, LICENSE_KEY). Cannot proceed.")
        return False
        
    # 2. Check local database cache
    local_valid_until = get_license_info()
    
    remote_check_needed = True
    
    if local_valid_until:
        time_left = local_valid_until - now
        
        if time_left.days >= MIN_REFRESH_INTERVAL_DAYS:
            # Local cache is fine, skip remote check
            remote_check_needed = False
        
        # If time_left is negative (already expired), remote_check_needed remains True

    if not remote_check_needed:
        return True # Local license is valid and sufficiently far from expiry

    # 3. Perform remote check
    payload = {
        "client_id": CLIENT_ID,
        "license_key": LICENSE_KEY,
    }

    try:
        print("[info] 🔑 LICENSING: Performing remote validation...")
        res = requests.post(LICENSE_SERVER_URL, json=payload, timeout=10)

        if res.status_code == 200:
            # Success! Extend the local expiry date.
            
            # Use the later of 'now' or the existing local expiry as the start point for extension.
            # This prevents penalizing the user if the cron job stops for a while.
            base_date = local_valid_until if local_valid_until and local_valid_until > now else now

            # Calculate the new expiry date: base date + LICENSE_EXTENSION_MONTHS
            new_valid_until = base_date + datetime.timedelta(days=30 * LICENSE_EXTENSION_MONTHS)
            
            # The server response (data.get("valid_until")) is ignored since we are extending, 
            # but we trust the 200 status code to proceed.
            
            update_license_info(new_valid_until)
            return True

        elif res.status_code == 403:
            # Remote server rejects the license (expired/invalid)
            data = res.json()
            error_msg = data.get("error", "Subscription expired or invalid key.")
            print(f"[error] ❌ LICENSING: Remote check rejected: {error_msg}")
            
            # Crucially, we do NOT extend or update the local DB.
            return False
            
        else:
            print(f"[error] ❌ LICENSING: Server returned status code {res.status_code}. Response: {res.text}")
            return False

    except requests.exceptions.RequestException as e:
        print(f"[error] ❌ LICENSING: Failed to connect to licensing server: {e}")
        
        # Server is unreachable. Check if we can rely on an unexpired local license.
        if local_valid_until and local_valid_until > now:
             print("[warn] ⚠️ LICENSING: Connection failed, but using *unexpired* local DB license cache.")
             return True
             
        print("[error] ❌ LICENSING: Connection failed and local license is expired or missing. Cannot proceed.")
        return False
    except Exception as e:
        print(f"[error] ❌ LICENSING: An unexpected error occurred during remote check: {e}")
        return False

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
            # Check License before proceeding 
            if not check_license():
                # Raise an exception if the license check fails
                raise Exception("License check failed. Terminating operation.")
                
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
            # Use 403 Forbidden for license errors
            error_message = str(e)
            if "License check failed" in error_message:
                self.send_response(403)
            else:
                self.send_response(500)
                
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": error_message}).encode())

# ==================================
# 🗄️ DATABASE (Product query uses get_db_connection now)
# ==================================
def get_products_from_db():
    print("[info] Connecting to database...")
    # NOTE: psycopg2 should be installed if running this locally: pip install psycopg2-binary
    conn = get_db_connection()
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
    # --- Check License before proceeding ---
    if not check_license():
        # Raise an exception if the license check fails
        raise Exception("License check failed. Terminating operation.")
        
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
        elif product["storeType"] == "flipkart":
            flip_total += 1
            for pincode in PINCODES_TO_CHECK:
                result = check_flipkart(product, pincode)
                if result:
                    flip_count += 1
                    in_stock.append(result)
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