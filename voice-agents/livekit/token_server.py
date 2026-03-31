"""Token generation server for Coval LiveKit integration.

This server provides an endpoint that Coval calls to get a LiveKit access token
for joining rooms and testing your voice agent.

Run with: python token_server.py
"""
import os
import logging
from flask import Flask, request, jsonify
from livekit.api import AccessToken, VideoGrants
from dotenv import load_dotenv

load_dotenv(".env.local")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("token-server")

app = Flask(__name__)

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")


@app.route("/token", methods=["POST"])
def generate_token():
    """Generate a LiveKit token for Coval to join a room.

    Coval sends:
        {
            "room_name": "uuid-generated-by-coval",
            "participant_name": "simulated_user"
        }

    We return:
        {
            "token": "jwt-token",
            "serverUrl": "wss://...",
            "room_name": "..."
        }
    """
    data = request.json or {}

    room_name = data.get("room_name", "default-room")
    participant_name = data.get("participant_name", "participant")

    logger.info(f"=== Token Request ===")
    logger.info(f"Room: {room_name}")
    logger.info(f"Participant: {participant_name}")
    logger.info(f"Full request body: {data}")

    # Create access token
    token = AccessToken(
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET,
    )
    token = token.with_identity(participant_name)
    token = token.with_name(participant_name)
    token = token.with_grants(VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    ))

    jwt_token = token.to_jwt()

    response = {
        "token": jwt_token,
        "serverUrl": LIVEKIT_URL,  # Coval looks for this field
        "room_name": room_name,
    }

    logger.info(f"Generated token successfully")
    logger.info(f"Response: serverUrl={LIVEKIT_URL}, room={room_name}")

    return jsonify(response)


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "livekit_url": LIVEKIT_URL})


if __name__ == "__main__":
    print("=" * 50)
    print("LiveKit Token Server for Coval Integration")
    print("=" * 50)
    print(f"LiveKit URL: {LIVEKIT_URL}")
    print(f"API Key: {LIVEKIT_API_KEY}")
    print(f"Token endpoint: http://localhost:8888/token")
    print("=" * 50)
    app.run(host="0.0.0.0", port=8888, debug=True)
