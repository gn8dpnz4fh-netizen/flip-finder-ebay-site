
import os
import hashlib
import logging

from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

VERIFICATION_TOKEN = os.environ.get("EBAY_VERIFICATION_TOKEN", "")
ENDPOINT_URL = os.environ.get("EBAY_ENDPOINT_URL", "")


@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "service": "Flip Finder notifications",
        "status": "running"
    }), 200


@app.route("/ebay/notifications", methods=["GET"])
def verify_endpoint():
    challenge_code = request.args.get("challenge_code")

    if not challenge_code:
        return jsonify({
            "error": "Missing challenge_code"
        }), 400

    if not VERIFICATION_TOKEN or not ENDPOINT_URL:
        return jsonify({
            "error": "Endpoint not configured"
        }), 503

    digest = hashlib.sha256(
        (
            challenge_code
            + VERIFICATION_TOKEN
            + ENDPOINT_URL
        ).encode("utf-8")
    ).hexdigest()

    return jsonify({
        "challengeResponse": digest
    }), 200


@app.route("/ebay/notifications", methods=["POST"])
def receive_notification():
    logging.warning(
        "Notification received, but secure processing "
        "has not been configured."
    )

    return jsonify({
        "error": "Secure processing is not configured"
    }), 503


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )
