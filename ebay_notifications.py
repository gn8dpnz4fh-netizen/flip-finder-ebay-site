import os
import json
import time
import base64
import hashlib
import logging
import hmac
import urllib.parse
import urllib.request
import urllib.error
from collections import deque, defaultdict
from threading import Lock
from datetime import datetime

from flask import Flask, request, jsonify
from openai import OpenAI
import stripe

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
logging.basicConfig(level=logging.INFO)

VERIFICATION_TOKEN = os.environ.get("EBAY_VERIFICATION_TOKEN", "")
ENDPOINT_URL = os.environ.get("EBAY_ENDPOINT_URL", "")
FLIPFINDER_API_TOKEN = os.environ.get("FLIPFINDER_API_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
EBAY_MARKETPLACE_ID = os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_US")

# Stripe subscription checkout. Keep STRIPE_SECRET_KEY only in Render's
# environment variables; never put it in frontend code or commit it to GitHub.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY", ""
).strip()
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://flip-finder-web.onrender.com").rstrip("/")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def verify_supabase_user():
    """Verify the signed-in user using Supabase Auth."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header.split(" ", 1)[1].strip()
    if not token or not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
        return None

    req = urllib.request.Request(
        SUPABASE_URL + "/auth/v1/user",
        headers={
            "Authorization": "Bearer " + token,
            "apikey": SUPABASE_PUBLISHABLE_KEY,
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            user = json.loads(response.read().decode("utf-8"))
        if isinstance(user, dict) and user.get("id"):
            return user
    except Exception:
        logging.warning("Supabase authentication verification failed")

    return None

ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://flip-finder-web.onrender.com").rstrip("/")
notifications = deque(maxlen=200)
notifications_lock = Lock()
request_times = defaultdict(deque)
rate_lock = Lock()
AI_REQUEST_LIMIT = 15
AI_WINDOW_SECONDS = 3600

# Free first version: direct eBay API if credentials/access are configured;
# other marketplaces are represented by direct search links, not scraped.
MARKETPLACES = {
    "eBay": "https://www.ebay.com/sch/i.html?_nkw=",
    "Facebook Marketplace": "https://www.facebook.com/marketplace/search/?query=",
    "Mercari": "https://www.mercari.com/search/?keyword=",
    "OfferUp": "https://offerup.com/search?q=",
    "Poshmark": "https://poshmark.com/search?query=",
    "Amazon": "https://www.amazon.com/s?k=",
    "Etsy": "https://www.etsy.com/search?q=",
    "Craigslist": "https://www.craigslist.org/search/sss?query=",
}

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin == ALLOWED_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

@app.route("/", methods=["GET"])
def health_check():
    from flask import send_from_directory
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.route("/<path:filename>", methods=["GET"])
def serve_frontend_files(filename):
    from flask import send_from_directory
    if filename in ("privacy.html", "accepted.html", "declined.html"):
        return send_from_directory(os.path.dirname(os.path.abspath(__file__)), filename)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/health", methods=["GET"])
def api_health_check():
    return jsonify({"service": "Flip Finder AI and marketplace pricing", "status": "running"}), 200


@app.route("/api/create-checkout-session", methods=["POST", "OPTIONS"])
def create_checkout_session():
    """Create a Stripe Checkout subscription with a 7-day trial."""
    if request.method == "OPTIONS":
        return "", 204

    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        return jsonify({
            "success": False,
            "error": "Stripe is not configured. Check STRIPE_SECRET_KEY and STRIPE_PRICE_ID in Render.",
        }), 503

    user = verify_supabase_user()
    if not user:
        return jsonify({
            "success": False,
            "error": "Please sign in to your Flip Finder account before starting your Pro trial.",
        }), 401

    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Expected JSON request."}), 400

    email = str(data.get("email", "")).strip()[:254]
    if email and ("@" not in email or len(email) < 3):
        return jsonify({
            "success": False,
            "error": "Please provide a valid email address.",
        }), 400

    try:
        session_args = {
            "mode": "subscription",
            "client_reference_id": user["id"],
            "metadata": {"supabase_user_id": user["id"]},
            "line_items": [{"price": STRIPE_PRICE_ID, "quantity": 1}],
            "payment_method_collection": "always",
            "subscription_data": {
                "trial_period_days": 7,
                "metadata": {"supabase_user_id": user["id"]},
            },
            "success_url": FRONTEND_URL + "/?subscription=success&session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": FRONTEND_URL + "/?subscription=cancelled",
            "allow_promotion_codes": True,
        }
        if email:
            session_args["customer_email"] = email

        session = stripe.checkout.Session.create(**session_args)
        return jsonify({"success": True, "checkout_url": session.url}), 200

    except stripe.error.StripeError:
        logging.exception("Stripe Checkout session creation failed")
        return jsonify({
            "success": False,
            "error": "Could not start checkout. Check the Stripe test price and backend configuration.",
        }), 502
    except Exception:
        logging.exception("Unexpected Stripe Checkout error")
        return jsonify({
            "success": False,
            "error": "Could not start checkout. Please try again.",
        }), 502


@app.route("/ebay/notifications", methods=["GET"])
def verify_endpoint():
    challenge_code = request.args.get("challenge_code")
    if not challenge_code:
        return jsonify({"error": "Missing challenge_code"}), 400
    if not VERIFICATION_TOKEN or not ENDPOINT_URL:
        logging.error("eBay verification settings missing")
        return jsonify({"error": "Endpoint not configured"}), 503
    digest = hashlib.sha256((challenge_code + VERIFICATION_TOKEN + ENDPOINT_URL).encode("utf-8")).hexdigest()
    return jsonify({"challengeResponse": digest}), 200

@app.route("/ebay/notifications", methods=["POST"])
def receive_notification():
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "Expected JSON"}), 400
    item = {"received_at": datetime.now().astimezone().isoformat(timespec="seconds"), "payload": payload}
    with notifications_lock:
        notifications.appendleft(item)
    logging.info("eBay notification received")
    return "", 200

@app.route("/api/notifications", methods=["GET"])
def get_notifications():
    if not FLIPFINDER_API_TOKEN:
        return jsonify({"error": "API not configured"}), 503
    auth_header = request.headers.get("Authorization", "")
    expected_header = f"Bearer {FLIPFINDER_API_TOKEN}"
    if not hmac.compare_digest(auth_header, expected_header):
        return jsonify({"error": "Unauthorized"}), 401
    with notifications_lock:
        result = list(notifications)
    return jsonify({"count": len(result), "notifications": result}), 200

def rate_limit_exceeded():
    now = time.time()
    ip = request.remote_addr or "unknown"
    with rate_lock:
        times = request_times[ip]
        while times and now - times[0] > AI_WINDOW_SECONDS:
            times.popleft()
        if len(times) >= AI_REQUEST_LIMIT:
            return True
        times.append(now)
    return False

@app.errorhandler(413)
def request_too_large(error):
    return jsonify({"error": "Photo is too large. Please use a smaller image."}), 413

@app.route("/api/analyze-image", methods=["POST", "OPTIONS"])
def analyze_image():
    if request.method == "OPTIONS":
        return "", 204
    if not OPENAI_API_KEY:
        return jsonify({"error": "AI service is not configured on the server."}), 503
    if rate_limit_exceeded():
        return jsonify({"error": "Too many scans. Please try again later."}), 429
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON request"}), 400
    photo = data.get("image", "")
    item_hint = str(data.get("itemName", "")).strip()[:200]
    if not isinstance(photo, str) or not photo:
        return jsonify({"error": "Please provide an item photo."}), 400
    allowed_prefixes = ("data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,")
    if not any(photo.startswith(p) for p in allowed_prefixes):
        return jsonify({"error": "Use a JPEG, PNG, or WebP image."}), 400
    if len(photo) > 7_000_000:
        return jsonify({"error": "Photo is too large. Choose a smaller image."}), 413
    prompt = f"""You are Flip Finder's careful product-identification assistant for resale research.
Your job is to identify the specific item visible in the image so a separate marketplace search can find close matches.

Accuracy rules:
- Describe only details supported by visible evidence or the user's hint. Never invent a brand, model, year, material, authenticity, rarity, or condition.
- Separate observed facts from uncertain guesses. Use "Unknown" for details that cannot be read.
- Inspect logos, labels, model numbers, serial/model text, dimensions, distinctive shapes, packaging, and visible markings when legible.
- If the item may be a coin, record denomination, date, mint mark, country, visible variety clues, and condition indicators only when actually visible. Do not claim a rare variety or grade from an ordinary photo.
- For electronics/tools, prioritize brand and exact model/part number. For clothing/shoes, prioritize brand, model line, size/tag details and style code. For collectibles, prioritize maker, series, edition, year and identifying marks.
- Condition observations must be limited to visible wear, damage, missing parts, and completeness; do not claim the item works or is authentic from appearance alone.
- Confidence is confidence in the identification, not confidence in price. Lower confidence when important markings are hidden, blurry, or ambiguous.
- Create a concise search_query for marketplace research. Include brand + exact model/series + important variant details when visible. Do not add unverified details. If the exact model is uncertain, use a broader query and explain why.
- Do not estimate a market price from the photo alone.

Return valid JSON with exactly these keys:
item_name (short specific name),
brand (string or "Unknown"),
model (string or "Unknown"),
category (string),
identifying_details (array of short observed facts),
condition_observations (array of visible condition facts),
confidence (integer 0-100),
search_query (one concise, high-specificity marketplace search query),
search_terms (array of up to 4 useful alternate search phrases),
uncertain_details (array of details that need a clearer photo or user confirmation),
limitations (short string explaining key uncertainty or "None noted").

User-provided description: {item_hint or 'None provided'}"""
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": photo, "detail": "high"}
            ]}],
            text={"format": {"type": "json_object"}},
            max_output_tokens=700,
        )
        result = json.loads(response.output_text)
        return jsonify({"success": True, "analysis": result}), 200
    except json.JSONDecodeError:
        logging.exception("AI returned invalid JSON")
        return jsonify({"error": "AI returned an unreadable result. Please try again."}), 502
    except Exception:
        logging.exception("AI image analysis failed")
        return jsonify({"error": "AI analysis failed. Check server configuration and try again."}), 502

def ebay_access_token():
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        return None, "eBay API credentials are not configured."
    credentials = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
    req = urllib.request.Request(
        "https://api.ebay.com/identity/v1/oauth2/token", data=body, method="POST",
        headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            token_data = json.loads(response.read().decode())
            return token_data.get("access_token"), None
    except urllib.error.HTTPError as exc:
        logging.warning("eBay token request failed with HTTP %s", exc.code)
        return None, f"eBay token request returned HTTP {exc.code}. Check credentials and production API access."
    except Exception:
        logging.exception("eBay token request failed")
        return None, "Could not connect to eBay API."

def search_ebay(query):
    token, error = ebay_access_token()
    if not token:
        return {"marketplace": "eBay", "status": "unavailable", "message": error, "results": []}
    params = urllib.parse.urlencode({"q": query, "limit": 10})
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search?" + params
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode())
        results = []
        for item in payload.get("itemSummaries", []):
            price = item.get("price") or {}
            shipping_options = item.get("shippingOptions") or []
            shipping = None
            if shipping_options:
                shipping = (shipping_options[0].get("shippingCost") or {}).get("value")
            results.append({
                "title": item.get("title", "Untitled listing"),
                "price": float(price["value"]) if price.get("value") else None,
                "currency": price.get("currency", "USD"),
                "shipping": float(shipping) if shipping is not None else None,
                "condition": item.get("condition"),
                "url": item.get("itemWebUrl"),
                "source": "eBay active listing",
            })
        return {"marketplace": "eBay", "status": "live" if results else "no_results", "message": "Active asking prices only; not completed-sale prices.", "results": results}
    except urllib.error.HTTPError as exc:
        logging.warning("eBay search failed with HTTP %s", exc.code)
        return {"marketplace": "eBay", "status": "unavailable", "message": f"eBay API returned HTTP {exc.code}. Production access may be restricted.", "results": []}
    except Exception:
        logging.exception("eBay search failed")
        return {"marketplace": "eBay", "status": "unavailable", "message": "Could not retrieve eBay listings.", "results": []}

@app.route("/api/marketplace-prices", methods=["POST", "OPTIONS"])
def marketplace_prices():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON request"}), 400
    query = str(data.get("itemName", data.get("query", ""))).strip()[:200]
    if not query:
        return jsonify({"error": "Provide an itemName or query."}), 400

    # Search eBay via its API when credentials and production access are available.
    ebay = search_ebay(query)
    # For other sites, supply one-click search links. Do not scrape their pages.
    links = [{"marketplace": name, "url": base + urllib.parse.quote_plus(query),
              "status": "search_link_only",
              "message": "This version provides a direct search link; it does not retrieve live prices from this marketplace."}
             for name, base in MARKETPLACES.items() if name != "eBay"]

    prices = [r["price"] + (r["shipping"] or 0) for r in ebay.get("results", []) if r.get("price") is not None]
    summary = None
    if prices:
        prices_sorted = sorted(prices)
        median = prices_sorted[len(prices_sorted) // 2] if len(prices_sorted) % 2 else (prices_sorted[len(prices_sorted)//2 - 1] + prices_sorted[len(prices_sorted)//2]) / 2
        summary = {
            "active_listing_count": len(prices),
            "lowest_active_total": round(min(prices), 2),
            "median_active_total": round(median, 2),
            "highest_active_total": round(max(prices), 2),
            "note": "These are active asking prices, not verified sold prices. Listing matches may differ in model, condition, or completeness."
        }
    return jsonify({
        "success": True,
        "query": query,
        "summary": summary,
        "marketplaces": [ebay] + links,
        "coverage": {
            "live_price_sources": ["eBay"] if ebay.get("status") == "live" else [],
            "search_links_only": [x["marketplace"] for x in links],
            "completed_sale_data": "Not included in this free starter version.",
            "pricing_warning": "Do not treat active asking prices as guaranteed resale value. Verify model, condition, shipping, fees, and sold prices before buying."
        }
    }), 200


# ============================================================
# SOLDGRAPH SOLD-PRICE SEARCH
# Website calls GET /api/soldgraph-test?q=...
# Soldgraph cache misses return 202 + poll_url; poll until complete.
# ============================================================
SOLDGRAPH_KEY = os.environ.get("SOLDGRAPH_KEY", "").strip()
SOLDGRAPH_BASE = "https://api.soldgraph.com"


def soldgraph_request(path, params=None, timeout=28):
    url = SOLDGRAPH_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {SOLDGRAPH_KEY}",
        "Accept": "application/json",
        "User-Agent": "FlipFinder/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8")), response.status, response.headers


@app.route("/api/soldgraph-test", methods=["GET", "OPTIONS"])
def soldgraph_test():
    if request.method == "OPTIONS":
        return "", 204
    if not SOLDGRAPH_KEY:
        return jsonify({"ok": False, "error": "SoldGraph is not configured. Add SOLDGRAPH_KEY to the backend service's environment variables."}), 503
    query = str(request.args.get("q", "")).strip()[:200]
    if not query:
        return jsonify({"ok": False, "error": "Enter an item name to search."}), 400
    try:
        payload, status, headers = soldgraph_request("/v1/ebay/sold", {"q": query, "count": 120, "sort": "recently_sold", "item_location": "domestic"}, timeout=28)
        # SoldGraph may return cached results immediately, or a job on cache miss.
        for _ in range(3):
            if payload.get("status") != "pending":
                break
            poll_url = payload.get("poll_url") or ("/v1/jobs/" + str(payload.get("request_id", "")))
            if not poll_url.startswith("/") or poll_url.startswith("//"):
                return jsonify({"ok": False, "error": "SoldGraph returned an invalid polling URL."}), 502
            payload, _, _ = soldgraph_request(poll_url, {"wait": 20}, timeout=28)
        state = payload.get("status")
        if state == "pending":
            return jsonify({"ok": False, "error": "SoldGraph is still preparing these results. Please try the search again in a few seconds.", "status": state}), 504
        if state == "failed":
            error = payload.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            return jsonify({"ok": False, "error": message or "SoldGraph could not complete this search.", "details": error}), 502
        if state != "complete" or not isinstance(payload.get("result"), dict):
            return jsonify({"ok": False, "error": "SoldGraph returned an unexpected response. Check the API key and endpoint."}), 502
        result = payload["result"]
        # Return a frontend-friendly object while preserving raw SoldGraph data.
        rows = result.get("data") if isinstance(result.get("data"), list) else []
        return jsonify({"ok": True, "status": "complete", "query": query, "summary": result.get("summary", {}), "count": len(rows), "result": result}), 200
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:1200]
        except Exception:
            pass
        logging.warning("SoldGraph returned HTTP %s: %s", exc.code, detail)
        if exc.code in (401, 403):
            message = "SoldGraph rejected the API key. Check SOLDGRAPH_KEY in Render."
        elif exc.code == 429:
            message = "SoldGraph rate or usage limit reached. Check your SoldGraph account."
        else:
            message = f"SoldGraph returned HTTP {exc.code}. Check the backend logs for details."
        return jsonify({"ok": False, "error": message, "details": detail}), 502
    except Exception:
        logging.exception("SoldGraph sold-price search failed")
        return jsonify({"ok": False, "error": "Could not connect to SoldGraph. Check backend logs and API configuration."}), 502

@app.route("/api/estimate-price", methods=["POST", "OPTIONS"])
def estimate_price():
    """Return a clearly labeled rough AI estimate when live pricing sources fail."""
    if request.method == "OPTIONS":
        return "", 204
    if not OPENAI_API_KEY:
        return jsonify({"success": False, "error": "AI price estimate is not configured. Add OPENAI_API_KEY to the backend environment."}), 503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Expected JSON request."}), 400
    item_name = str(data.get("itemName", "")).strip()[:300]
    condition = str(data.get("condition", "unknown")).strip()[:200]
    if not item_name:
        return jsonify({"success": False, "error": "Provide an item name."}), 400
    if rate_limit_exceeded():
        return jsonify({"success": False, "error": "Too many AI requests. Please wait and try again."}), 429
    prompt = f"""You are Flip Finder's fallback resale estimator. Live marketplace data was unavailable.
Give a cautious rough US-dollar resale estimate for this item, not a claim about current live market prices.
Item identification/search name: {item_name}
Condition: {condition}
Rules:
- If the name is too vague to price responsibly, give a broad plausible range and low confidence; do not pretend it is the exact model.
- Do not claim you checked eBay, sold listings, or the current web.
- Consider common used resale value, not retail price. Avoid extreme rare/collector premiums unless explicitly identified.
- All prices must be positive USD numbers. Keep low <= typical <= high.
- Confidence is 0-100 and should be low when model/condition is ambiguous or the item is rare/collectible.
Return valid JSON with keys: typical_resale_price (number), low_price (number), high_price (number), confidence (integer 0-100), assumptions (array of short strings), identification_needed (array of missing details that would materially change value), search_query (string).
"""
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            text={"format": {"type": "json_object"}},
            max_output_tokens=450,
        )
        estimate = json.loads(response.output_text)
        typical = float(estimate.get("typical_resale_price", 0))
        low = float(estimate.get("low_price", 0))
        high = float(estimate.get("high_price", 0))
        if not (0 < low <= typical <= high and high < 100000000):
            return jsonify({"success": False, "error": "AI did not return a usable price range."}), 502
        estimate["typical_resale_price"] = round(typical, 2)
        estimate["low_price"] = round(low, 2)
        estimate["high_price"] = round(high, 2)
        estimate["basis"] = "AI rough estimate only; no live marketplace data was available."
        return jsonify({"success": True, "estimate": estimate, "query": item_name}), 200
    except Exception:
        logging.exception("AI fallback price estimate failed")
        return jsonify({"success": False, "error": "AI fallback estimate failed. Check the OpenAI API key, model access, and backend logs."}), 502

def update_supabase_profile(user_id, customer_id, subscription_id, status, trial_end=None):
    """Upsert the user's Stripe subscription status into Supabase."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase server configuration is missing")

    profile = {
        "id": user_id,
        "subscription_status": "pro" if status in ("trialing", "active") else "free",
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    if customer_id:
        profile["stripe_customer_id"] = customer_id
    if subscription_id:
        profile["stripe_subscription_id"] = subscription_id
    if trial_end:
        profile["trial_ends_at"] = trial_end

    req = urllib.request.Request(
        SUPABASE_URL + "/rest/v1/profiles?on_conflict=id",
        data=json.dumps(profile).encode("utf-8"),
        method="POST",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": "Bearer " + SUPABASE_SERVICE_ROLE_KEY,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        if response.status not in (200, 201, 204):
            raise RuntimeError("Supabase profile update failed")


def timestamp_to_iso(timestamp):
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp).astimezone().isoformat()


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        logging.error("Stripe webhook secret is missing")
        return jsonify({"error": "Webhook not configured"}), 503

    try:
        event = stripe.Webhook.construct_event(
            request.get_data(),
            request.headers.get("Stripe-Signature", ""),
            webhook_secret,
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"error": "Invalid webhook"}), 400

    event_type = event["type"]
    obj = event["data"]["object"]

    try:
        if event_type == "checkout.session.completed":
            user_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("supabase_user_id")
            subscription_id = obj.get("subscription")
            if not user_id or not subscription_id:
                logging.warning("Checkout session missing user or subscription ID")
                return jsonify({"received": True}), 200

            subscription = stripe.Subscription.retrieve(subscription_id)
            update_supabase_profile(
                user_id,
                obj.get("customer"),
                subscription_id,
                subscription.get("status", "incomplete"),
                timestamp_to_iso(subscription.get("trial_end")),
            )

        elif event_type in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            user_id = (obj.get("metadata") or {}).get("supabase_user_id")
            if not user_id:
                logging.warning("Subscription event missing Supabase user ID")
                return jsonify({"received": True}), 200

            update_supabase_profile(
                user_id,
                obj.get("customer"),
                obj.get("id"),
                obj.get("status", "canceled"),
                timestamp_to_iso(obj.get("trial_end")),
            )

        logging.info("Stripe webhook processed: %s", event_type)
        return jsonify({"received": True}), 200
    except Exception:
        logging.exception("Could not sync Stripe subscription to Supabase")
        return jsonify({"error": "Subscription sync failed"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
