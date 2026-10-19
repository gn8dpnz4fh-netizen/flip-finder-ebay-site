
import os
import json
import hashlib
import logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import (
    urlencode,
    urljoin,
    urlparse,
    parse_qsl,
    urlunparse,
)

from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

VERIFICATION_TOKEN = os.environ.get("EBAY_VERIFICATION_TOKEN", "")
ENDPOINT_URL = os.environ.get("EBAY_ENDPOINT_URL", "")
SOLDGRAPH_BASE = "https://api.soldgraph.com"


# ==================================================
# HEALTH CHECK
# ==================================================

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "service": "Flip Finder notifications",
        "status": "running",
    }), 200


# ==================================================
# EBAY NOTIFICATION ENDPOINT
# ==================================================

@app.route("/ebay/notifications", methods=["GET"])
def verify_endpoint():
    challenge_code = request.args.get("challenge_code")

    if not challenge_code:
        return jsonify({
            "error": "Missing challenge_code",
        }), 400

    if not VERIFICATION_TOKEN or not ENDPOINT_URL:
        return jsonify({
            "error": "Endpoint not configured",
        }), 503

    digest = hashlib.sha256(
        (
            challenge_code
            + VERIFICATION_TOKEN
            + ENDPOINT_URL
        ).encode("utf-8")
    ).hexdigest()

    return jsonify({
        "challengeResponse": digest,
    }), 200


@app.route("/ebay/notifications", methods=["POST"])
def receive_notification():
    logging.warning(
        "Notification received, but secure processing "
        "has not been configured."
    )

    return jsonify({
        "error": "Secure processing is not configured",
    }), 503


# ==================================================
# SOLDGRAPH API HELPER
# ==================================================

def soldgraph_request(url, api_key, timeout=25):
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)

    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = {}

        return exc.code, body

    except (URLError, TimeoutError):
        raise


# ==================================================
# SOLDGRAPH TEST ENDPOINT
# ==================================================

@app.route("/api/soldgraph-test", methods=["GET"])
def soldgraph_test():
    api_key = os.environ.get("SOLDGRAPH_KEY", "").strip()

    if not api_key:
        return jsonify({
            "ok": False,
            "error": "SOLDGRAPH_KEY is not configured in Render.",
        }), 500

    query = request.args.get("q", "").strip()

    if not query or len(query) > 200:
        return jsonify({
            "ok": False,
            "error": "Provide a search query between 1 and 200 characters.",
            "example": "/api/soldgraph-test?q=nintendo%20switch%20oled",
        }), 400

    try:
        params = urlencode({
            "q": query,
            "count": 200,
            "sort": "recently_sold",
            "item_location": "domestic",
        })

        url = f"{SOLDGRAPH_BASE}/v1/ebay/sold?{params}"

        status, data = soldgraph_request(url, api_key)

        # Handle pending Soldgraph search jobs.
        for _ in range(3):
            if status != 202 or data.get("status") != "pending":
                break

            poll_path = data.get("poll_url")

            if not poll_path:
                break

            poll_url = urljoin(SOLDGRAPH_BASE, poll_path)
            parsed = urlparse(poll_url)

            # Restrict polling to the official Soldgraph API host.
            if (
                parsed.scheme != "https"
                or parsed.hostname != "api.soldgraph.com"
            ):
                return jsonify({
                    "ok": False,
                    "error": "Unexpected Soldgraph polling URL.",
                }), 502

            poll_params = [
                (key, value)
                for key, value in parse_qsl(parsed.query)
                if key != "wait"
            ]
            poll_params.append(("wait", "20"))

            poll_url = urlunparse(
                parsed._replace(
                    query=urlencode(poll_params)
                )
            )

            status, data = soldgraph_request(
                poll_url,
                api_key,
                timeout=25,
            )

        if status not in (200, 202):
            logging.error(
                "Soldgraph returned HTTP %s", status
            )

            return jsonify({
                "ok": False,
                "error": "Soldgraph request failed.",
                "http_status": status,
                "details": data.get("error", {}),
            }), 502

        if data.get("status") == "pending":
            return jsonify({
                "ok": False,
                "status": "pending",
                "message": "Search is still processing. Try again shortly.",
            }), 202

        if data.get("status") == "failed":
            return jsonify({
                "ok": False,
                "error": data.get("error", {}),
            }), 502

        result = data.get("result", {})
        summary = result.get("summary", {})

        return jsonify({
            "ok": True,
            "query": query,
            "source": result.get("provider", "ebay"),
            "data_type": "sold listings",
            "summary": {
                "priced_count": summary.get("priced_count"),
                "median": summary.get("median"),
                "low": summary.get("low"),
                "high": summary.get("high"),
                "p25": summary.get("p25"),
                "p75": summary.get("p75"),
                "currency": summary.get("currency"),
            },
            "listings": [
                {
                    "title": item.get("title"),
                    "price": item.get("displayed_price"),
                    "sold_date": item.get("sold_date"),
                    "condition": item.get("condition"),
                    "link": item.get("link"),
                }
                for item in result.get("data", [])[:20]
            ],
        }), 200

    except (URLError, TimeoutError):
        logging.exception("Soldgraph connection failed")

        return jsonify({
            "ok": False,
            "error": "Could not connect to Soldgraph. Check Render logs.",
        }), 504

    except Exception:
        logging.exception("Soldgraph test failed")

        return jsonify({
            "ok": False,
            "error": "Unexpected server error. Check Render logs.",
        }), 500


# ==================================================
# START SERVER
# ==================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
