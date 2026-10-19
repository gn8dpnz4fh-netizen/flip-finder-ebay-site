
import os
import json
import hashlib
import logging
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

VERIFICATION_TOKEN = os.environ.get("EBAY_VERIFICATION_TOKEN", "")
ENDPOINT_URL = os.environ.get("EBAY_ENDPOINT_URL", "")

SOLDGRAPH_KEY = os.environ.get("SOLDGRAPH_KEY", "").strip()
SOLDGRAPH_BASE = "https://api.soldgraph.com"


@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "service": "Flip Finder notifications",
        "status": "running"
    }), 200


# eBay notification endpoint verification
@app.route("/ebay/notifications", methods=["GET"])
def verify_endpoint():
    challenge_code = request.args.get("challenge_code")

    if not challenge_code:
        return jsonify({"error": "Missing challenge_code"}), 400

    if not VERIFICATION_TOKEN or not ENDPOINT_URL:
        return jsonify({"error": "Endpoint not configured"}), 503

    digest = hashlib.sha256(
        (challenge_code + VERIFICATION_TOKEN + ENDPOINT_URL).encode("utf-8")
    ).hexdigest()

    return jsonify({"challengeResponse": digest}), 200


# Existing eBay notification handler
@app.route("/ebay/notifications", methods=["POST"])
def receive_notification():
    logging.warning(
        "eBay notification received, but secure processing "
        "has not been configured."
    )
    return jsonify({
        "error": "Secure processing is not configured"
    }), 503


# Soldgraph API test endpoint
@app.route("/api/soldgraph-test", methods=["GET"])
def soldgraph_test():
    api_key = os.environ.get("SOLDGRAPH_KEY", "").strip()

    if not api_key:
        return jsonify({
            "ok": False,
            "error": "SOLDGRAPH_KEY is not configured in Render."
        }), 500

    query = request.args.get("q", "").strip()

    if not query or len(query) > 200:
        return jsonify({
            "ok": False,
            "error": "Provide a search query between 1 and 200 characters.",
            "example": "/api/soldgraph-test?q=nintendo%20switch%20oled"
        }), 400

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "FlipFinder/1.0"
    }

    params = urlencode({
        "q": query,
        "count": 200,
        "page": 1,
        "sort": "recently_sold",
        "item_location": "domestic"
    })

    api_url = f"{SOLDGRAPH_BASE}/v1/ebay/sold?{params}"

    try:
        req = Request(api_url, headers=headers, method="GET")

        try:
            with urlopen(req, timeout=60) as response:
                status_code = response.status
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            status_code = exc.code
            body = exc.read().decode("utf-8", errors="replace")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return jsonify({
                "ok": False,
                "error": "Soldgraph returned a non-JSON response.",
                "upstream_status": status_code
            }), 502

        # Soldgraph may return a pending job that must be polled.
        if data.get("status") == "pending":
            poll_url = data.get("poll_url")

            if not poll_url:
                return jsonify({
                    "ok": False,
                    "error": "Soldgraph returned a pending job without poll_url.",
                    "request_id": data.get("request_id")
                }), 502

            # poll_url is a path on api.soldgraph.com.
            poll_url = urljoin(SOLDGRAPH_BASE, poll_url)
            separator = "&" if "?" in poll_url else "?"
            poll_url = f"{poll_url}{separator}wait=20"

            poll_req = Request(
                poll_url,
                headers=headers,
                method="GET"
            )

            try:
                with urlopen(poll_req, timeout=60) as response:
                    status_code = response.status
                    body = response.read().decode("utf-8")
            except HTTPError as exc:
                status_code = exc.code
                body = exc.read().decode("utf-8", errors="replace")

            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return jsonify({
                    "ok": False,
                    "error": "Soldgraph polling returned non-JSON data.",
                    "upstream_status": status_code
                }), 502

        if data.get("status") == "failed":
            return jsonify({
                "ok": False,
                "error": "Soldgraph search failed.",
                "details": data.get("error", {})
            }), 502

        if status_code >= 400:
            # Do not return request headers or API keys.
            return jsonify({
                "ok": False,
                "error": "Soldgraph API request failed.",
                "upstream_status": status_code,
                "details": data.get("error", data)
            }), 502

        if data.get("status") == "pending":
            return jsonify({
                "ok": True,
                "status": "pending",
                "message": "Search is still processing. Try again shortly.",
                "request_id": data.get("request_id")
            }), 202

        result = data.get("result", {})

        return jsonify({
            "ok": True,
            "status": data.get("status", "complete"),
            "query": query,
            "request_id": data.get("request_id"),
            "summary": result.get("summary", {}),
            "data": result.get("data", []),
            "count": len(result.get("data", [])),
            "credits": data.get("credits"),
            "cached": data.get("cached")
        }), 200

    except HTTPError as exc:
        logging.exception("Soldgraph HTTP error")
        return jsonify({
            "ok": False,
            "error": "Soldgraph request failed.",
            "upstream_status": exc.code
        }), 502

    except (URLError, TimeoutError):
        logging.exception("Soldgraph connection error")
        return jsonify({
            "ok": False,
            "error": "Could not connect to Soldgraph. Try again later."
        }), 502

    except Exception:
        logging.exception("Unexpected Soldgraph error")
        return jsonify({
            "ok": False,
            "error": "Unexpected server error while testing Soldgraph."
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
