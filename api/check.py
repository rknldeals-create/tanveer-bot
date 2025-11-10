from http.server import BaseHTTPRequestHandler
import os, json, requests, psycopg2, datetime, hashlib, hmac, time
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup, Comment

# ==================================
# 🔧 CONFIGURATION
# ==================================
PINCODES_TO_CHECK = ["132001"]
DATABASE_URL = os.getenv("DATABASE_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_GROUP_ID = "-4789301236"  # Telegram group ID
CRON_SECRET = os.getenv("CRON_SECRET")

# Amazon credentials
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AMAZON_PARTNER_TAG = os.getenv("AMAZON_PARTNER_TAG")

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

            final_message = (
                "🔥 *Stock Alert!*\n\n" + "\n\n".join(in_stock_messages) + "\n\n" + summary
                if in_stock_messages
                else "❌ *No stock available currently.*\n\n" + summary
            )

            send_telegram_message(final_message)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "found": len(in_stock_messages)}).encode())

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
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT name, url, product_id, store_type, affiliate_link FROM products")
    products = cursor.fetchall()
    conn.close()

    products_list = [
        {"name": row[0], "url": row[1], "productId": row[2], "storeType": row[3], "affiliateLink": row[4]}
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
        "disable_web_page_preview": True
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
                        "extn": {"widerStoreFlag": "N"}
                    }
                ]
            }
        }
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "oms-apim-subscription-key": "1131858141634e2abe2efb2b3a2a2a5d",
        "origin": "https://www.croma.com",
        "referer": "https://www.croma.com/"
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
# 🟣 FLIPKART ROME API CHECKER
# ==================================
def check_flipkart_api(product, pincode="132001"):
    """Direct call to Flipkart's Rome API (to test if Vercel IP works)."""
    try:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.flipkart.com",
            "Referer": "https://www.flipkart.com",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "X-User-Agent": (
                "Mozilla/5.0 FKUA/msite/0.0.3/msite/Mobile"
            ),
            "flipkart_secure": "true"
        }

        payload = {
            "requestContext": {
                "products": [{"productId": product["productId"]}],
                "marketplace": "FLIPKART"
            },
            "locationContext": {"pincode": pincode}
        }

        url = "https://2.rome.api.flipkart.com/api/3/product/serviceability"

        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code != 200:
            print(f"[FLIPKART] ⚠️ API call failed for {product['name']} ({res.status_code})")
            return None

        data = res.json()

        try:
            listing = data["RESPONSE"][product["productId"]]["listingSummary"]
            available = listing.get("available", False)
        except Exception:
            available = False

        if available:
            print(f"[FLIPKART] ✅ {product['name']} deliverable at {pincode}")
            return f"✅ *Flipkart*\n[{product['name']}]({product['affiliateLink'] or product['url']})"

        print(f"[FLIPKART] ❌ {product['name']} unavailable at {pincode}")
        return None

    except Exception as e:
        print(f"[error] Flipkart API failed for {product['name']}: {e}")
        return None

# ==================================
# 🧾 AMAZON CHECKER
# ==================================
def sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

def get_signature_key(key, date_stamp, region_name, service_name):
    k_date = sign(("AWS4" + key).encode("utf-8"), date_stamp)
    k_region = sign(k_date, region_name)
    k_service = sign(k_region, service_name)
    return sign(k_service, "aws4_request")

def check_amazon(product):
    asin = product["productId"]
    method = "POST"
    endpoint = "https://webservices.amazon.in/paapi5/getitems"
    region = "eu-west-1"
    service = "ProductAdvertisingAPI"
    t = datetime.datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")

    payload = {
        "ItemIds": [asin],
        "Resources": [
            "ItemInfo.Title",
            "Offers.Listings.Price",
            "Offers.Listings.Availability.Message"
        ],
        "PartnerTag": AMAZON_PARTNER_TAG,
        "PartnerType": "Associates",
        "Marketplace": "www.amazon.in"
    }

    canonical_uri = "/paapi5/getitems"
    canonical_headers = (
        f"content-encoding:amz-1.0\n"
        f"host:{urlparse(endpoint).netloc}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-target:com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems\n"
    )
    signed_headers = "content-encoding;host;x-amz-date;x-amz-target"
    payload_hash = hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()
    canonical_request = f"{method}\n{canonical_uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"{algorithm}\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    signing_key = get_signature_key(AWS_SECRET_ACCESS_KEY, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization_header = (
        f"{algorithm} Credential={AWS_ACCESS_KEY_ID}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    headers = {
        "Content-Encoding": "amz-1.0",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Amz-Date": amz_date,
        "X-Amz-Target": "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems",
        "Authorization": authorization_header,
        "Accept": "application/json, text/javascript",
        "Host": urlparse(endpoint).netloc,
    }

    try:
        res = requests.post(endpoint, headers=headers, data=json.dumps(payload), timeout=10)
        data = res.json()

        if res.status_code == 200 and "ItemsResult" in data:
            item = data["ItemsResult"]["Items"][0]
            title = item["ItemInfo"]["Title"]["DisplayValue"]
            availability = item["Offers"]["Listings"][0]["Availability"]["Message"]
            price = item["Offers"]["Listings"][0]["Price"]["DisplayAmount"]
            print(f"[AMAZON] ✅ {title} in stock.")
            return f"✅ *Amazon*\n[{title}]({product['affiliateLink'] or product['url']})\n💰 {price}\n📦 {availability}"

        if "TooManyRequests" in str(data):
            print(f"[AMAZON] ⚠️ Skipping {product['name']} (throttled).")
            return None

        print(f"[AMAZON] ⚠️ No stock info for {product['name']}")
    except Exception as e:
        print(f"[error] Amazon check failed for {product['name']}: {e}")
    return None

# ==================================
# 🚀 MAIN LOGIC
# ==================================
def main_logic():
    start_time = time.time()
    print("[info] Starting stock check...")
    products = get_products_from_db()
    in_stock = []
    croma_count = flip_count = amazon_count = 0
    croma_total = flip_total = amazon_total = 0

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
                result = check_flipkart_api(product, pincode)
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

    duration = round(time.time() - start_time, 2)
    summary = (
        f"🟢 *Croma:* {croma_count}/{croma_total}\n"
        f"🟣 *Flipkart:* {flip_count}/{flip_total}\n"
        f"🟡 *Amazon:* {amazon_count}/{amazon_total}\n"
        f"📦 *Total:* {len(in_stock)} available\n"
        f"⏱ *Time taken:* {duration}s"
    )

    print(f"[info] ✅ Found {len(in_stock)} products in stock.")
    print("[info] Summary:\n" + summary)
    return in_stock, summary
