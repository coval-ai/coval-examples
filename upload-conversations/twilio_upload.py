#!/usr/bin/env python3
"""
Twilio Call Recording Upload Example for Coval

This script demonstrates how to:
1. Fetch call recordings from Twilio
2. Get the recording URL with authentication
3. Upload the conversation to Coval for evaluation

Requirements:
    pip install twilio requests

Documentation:
    - Twilio Recordings API: https://www.twilio.com/docs/voice/api/recording
    - Coval API: https://docs.coval.dev/api-reference/v1/conversations/submit-conversation-for-evaluation
"""

import sys
from datetime import datetime

import requests
from twilio.rest import Client

# Configuration
TWILIO_ACCOUNT_SID = "your_twilio_account_sid"
TWILIO_AUTH_TOKEN = "your_twilio_auth_token"
COVAL_API_KEY = "your_coval_api_key"
CALL_SID = "your_call_sid"
METRIC_IDS = []

# Constants
API_TIMEOUT = 30
COVAL_API_URL = "https://api.coval.dev/v1/conversations:submit"


def get_twilio_call_recording(call_sid):
    """Fetch call details and recording from Twilio.

    Args:
        call_sid: Twilio call SID

    Returns:
        Dictionary containing call data or None if error
    """
    print(f"Fetching Twilio call details for SID: {call_sid}")

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    try:
        call = client.calls(call_sid).fetch()
        recordings = client.recordings.list(call_sid=call_sid, limit=1)

        if not recordings:
            print(f"Error: No recordings found for call {call_sid}")
            return None

        recording = recordings[0]
        recording_url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{TWILIO_ACCOUNT_SID}/Recordings/{recording.sid}.mp3"
        )

        call_data = {
            "call_sid": call_sid,
            "recording_sid": recording.sid,
            "recording_url": recording_url,
            "from_number": call.from_,
            "to_number": call.to,
            "duration": call.duration,
            "start_time": call.start_time,
            "status": call.status,
            "direction": call.direction,
        }

        print(f"Call recording found: {recording.sid}")
        print(f"Duration: {call.duration} seconds")
        print(f"From: {call.from_} -> To: {call.to}")

        return call_data

    except Exception as e:
        print(f"Error fetching Twilio call: {e}")
        return None


def upload_to_coval(call_data, audio_url=None):
    """Upload conversation to Coval for evaluation.

    Args:
        call_data: Dictionary containing call details
        audio_url: Optional presigned URL to audio file

    Returns:
        API response dictionary or None if error
    """
    print("\nUploading to Coval...")

    payload = {
        "external_conversation_id": call_data["call_sid"],
        "occurred_at": (
            call_data["start_time"].isoformat()
            if call_data["start_time"]
            else datetime.utcnow().isoformat()
        ),
        "metadata": {
            "platform": "twilio",
            "call_sid": call_data["call_sid"],
            "recording_sid": call_data["recording_sid"],
            "from": call_data["from_number"],
            "to": call_data["to_number"],
            "duration": str(call_data["duration"]),
            "status": call_data["status"],
            "direction": call_data["direction"],
        },
    }

    if audio_url:
        payload["audio_url"] = audio_url
        print(f"Audio URL: {audio_url[:60]}...")
    else:
        print("Warning: No audio URL provided")
        print(
            "Note: Download recording and upload to cloud storage (S3/GCS), "
            "then generate presigned URL"
        )

    if METRIC_IDS:
        payload["metrics"] = METRIC_IDS

    headers = {"X-API-Key": COVAL_API_KEY, "Content-Type": "application/json"}

    try:
        response = requests.post(
            COVAL_API_URL, json=payload, headers=headers, timeout=API_TIMEOUT
        )

        if response.status_code == 200:
            result = response.json()
            conversation_id = result.get("conversation", {}).get("conversation_id")
            status = result.get("conversation", {}).get("status")

            print("Successfully uploaded to Coval")
            print(f"Conversation ID: {conversation_id}")
            print(f"Status: {status}")
            print(f"View at: https://app.coval.dev/conversations/{conversation_id}")

            return result

        print(f"Failed to upload to Coval: {response.status_code}")
        print(f"Response: {response.text}")
        return None

    except requests.exceptions.Timeout:
        print(f"Error: Request timed out after {API_TIMEOUT} seconds")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error uploading to Coval: {e}")
        return None


def main():
    """Main execution function."""
    if TWILIO_ACCOUNT_SID == "your_twilio_account_sid":
        print("Error: Please update TWILIO_ACCOUNT_SID in the configuration section")
        sys.exit(1)

    if COVAL_API_KEY == "your_coval_api_key":
        print("Error: Please update COVAL_API_KEY in the configuration section")
        sys.exit(1)

    if CALL_SID == "your_call_sid":
        print("Error: Please update CALL_SID in the configuration section")
        sys.exit(1)

    call_data = get_twilio_call_recording(CALL_SID)
    if not call_data:
        sys.exit(1)

    # Note: Implement cloud storage upload to get audio_url
    audio_url = None

    result = upload_to_coval(call_data, audio_url)

    if not result:
        print("Upload failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
