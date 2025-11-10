from http.server import BaseHTTPRequestHandler
import os, json, requests, psycopg2, datetime, hashlib, hmac, time
from urllib.parse import urlparse, parse_qs

# --- 1. CONFIGURATION ---
PINCODES_TO_CHECK = ['132001']
DATABASE_URL = os.getenv('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CRON_SECRET = os.getenv('CRON_SECRET')

# Amazon credentials
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AMAZON_PARTNER_TAG = os.getenv("AMAZON_PARTNER_TAG")

# --- 2. VERCEL HANDLER ---
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query_components = parse_qs(urlparse(self.path).query)
        auth_key = query_components.get('secret', [None])[0]

        if auth_key != CRON_SECRET:
            self.send_response(401)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Unauthorized'}).encode())
            return

        try:
            in_stock_messages = main_logic()

            if in_stock_messages:
                print(f"Found {len(in_stock_messages)} items in stock. Sending Telegram message.")
                final_message = "🔥 *Stock Alert!*\n\n" + "\n\n".join(in_stock_messages)
                send_telegram_message(final_message)
            else:
                print("All items out of stock or API failures. Notification sent.")

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'found': len(in_stock_messages)}).encode())

        except Exception as e:
            print(f"Error: {e}")
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

# --- 3. DATABASE ---
def get_products_from_db():
    print("Connecting to database...")
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT name, url, product_id, store_type, affiliate_link FROM products")
    products = cursor.fetchall()
    conn.close()

    return [
        {"name": row[0], "url": row[1], "productId": row[2], "storeType": row[3], "affiliateLink": row[4]}
        for row in products
    ]

# --- 4. TELEGRAM HELPERS ---
def get_all_chat_ids():
    """Fetch all unique chat IDs that started the bot."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        updates = res.json().get("result", [])
        ids = {u["message"]["chat"]["id"] for u in updates if "message" in u and "chat" in u}
        print(f"Fetched {len(ids)} Telegram subscribers.")
        return list(ids)
    except Exception as e:
        print(f"⚠️ Failed to fetch Telegram chat IDs: {e}")
        return []

def send_telegram_message(message):
    """Send a Telegram message to all chat IDs dynamically fetched."""
    if not TELEGRAM_BOT_TOKEN:
        print("Telegram BOT TOKEN not set.")
        return

    chat_ids = get_all_chat_ids()
    print(f"Sending message to {len(chat_ids)} users...")

    for chat_id in chat_ids:
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown',
            'disable_web_page_preview': True
        }
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            res = requests.post(url, json=payload, timeout=5)
            if res.status_code != 200:
                print(f"⚠️ Telegram error for {chat_id}: {res.text}")
            time.sleep(0.5)  # prevent Telegram flood limit
        except Exception as e:
            print(f"❌ Failed to send to {chat_id}: {e}")

# --- 5. CROMA CHECKER ---
def check_croma(product, pincode):
    url = 'https://api.croma.com/inventory/oms/v2/tms/details-pwa/'
    payload = {
        "promise": {
            "allocationRuleID": "SYSTEM", "checkInventory": "Y", "organizationCode": "CROMA",
            "sourcingClassification": "EC",
            "promiseLines": {"promiseLine": [{
                "fulfillmentType": "HDEL", "itemID": product["productId"], "lineId": "1",
                "requiredQty": "1", "shipToAddress": {"zipCode": pincode},
                "extn": {"widerStoreFlag": "N"}
            }]}
        }
    }
    headers = {
        'accept': 'application/json', 'content-type': 'application/json',
        'oms-apim-subscription-key': '1131858141634e2abe2efb2b3a2a2a5d',
        'origin': 'https://www.croma.com', 'referer': 'https://www.croma.com/'
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        data = res.json()
        if data.get("promise", {}).get("suggestedOption", {}).get("option", {}).get("promiseLines"):
            return f'✅ *In Stock at Croma ({pincode})*\n[{product["name"]}]({product["affiliateLink"] or product["url"]})'
    except Exception as e:
        print(f"Croma check failed for {product['name']}: {e}")
    return None

# --- 6. AMAZON CHECKER ---
def sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

def get_signature_key(key, date_stamp, region_name, service_name):
    k_date = sign(("AWS4" + key).encode("utf-8"), date_stamp)
    k_region = sign(k_date, region_name)
    k_service = sign(k_region, service_name)
    return sign(k_service, "aws4_request")

def check_amazon(product):
    """Check Amazon availability via PAAPI (single request, no retry)"""
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
            link = product["affiliateLink"] or product["url"]
            return f"🛒 *Amazon*\n[{title}]({link})\n💰 {price}\n📦 {availability}"

        print(f"Amazon API Error: {data}")
    except Exception as e:
        print(f"Amazon check failed for {product['name']}: {e}")

    return None

# --- 7. MAIN LOGIC ---
def main_logic():
    print("Starting stock check...")
    products = get_products_from_db()
    in_stock_messages = []
    amazon_failures = 0

    for product in products:
        result = None
        if product["storeType"] == "croma":
            for pincode in PINCODES_TO_CHECK:
                result = check_croma(product, pincode)
                if result:
                    in_stock_messages.append(result)
                    break
        elif product["storeType"] == "amazon":
            result = check_amazon(product)
            if result:
                in_stock_messages.append(result)
            else:
                amazon_failures += 1

    # Always notify if nothing found
    if not in_stock_messages:
        msg = f"❌ No stock available currently.\nAmazon API failed for {amazon_failures}/{sum(1 for p in products if p['storeType']=='amazon')} products."
        print(msg)
        send_telegram_message(msg)

    return in_stock_messages
