#!/usr/bin/env python3
"""
Vapi AI Call Upload Example for Coval

This script demonstrates how to:
1. Fetch call data from Vapi AI API
2. Transform Vapi messages to Coval format (with role reversal)
3. Upload the conversation with transcript and audio to Coval

IMPORTANT: Vapi has reversed role naming:
- Vapi "user" = your AI assistant (maps to "assistant" in Coval)
- Vapi "bot" = end user/customer (maps to "user" in Coval)

Requirements:
    pip install requests

Documentation:
    - Vapi Calls API: https://docs.vapi.ai/api-reference/calls/get
    - Coval API: https://docs.coval.dev/api-reference/v1/conversations/submit-conversation-for-evaluation
"""

import sys
from datetime import datetime

import requests

# Configuration
VAPI_API_KEY = "your_vapi_api_key"
COVAL_API_KEY = "your_coval_api_key"
CALL_ID = "your_vapi_call_id"
METRIC_IDS = []

# Constants
API_TIMEOUT = 30
VAPI_API_URL = "https://api.vapi.ai/call"
COVAL_API_URL = "https://api.coval.dev/v1/conversations:submit"


def get_vapi_call(call_id):
    """Fetch call details from Vapi AI API.

    Args:
        call_id: Vapi call ID

    Returns:
        Dictionary containing call data or None if error
    """
    print(f"Fetching Vapi call details for ID: {call_id}")

    headers = {
        "Authorization": f"Bearer {VAPI_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(
            f"{VAPI_API_URL}/{call_id}", headers=headers, timeout=API_TIMEOUT
        )

        if response.status_code == 200:
            call_data = response.json()
            print("Call data retrieved successfully")
            print(f"Status: {call_data.get('status')}")
            print(f"Messages: {len(call_data.get('messages', []))} found")
            return call_data

        print(f"Failed to fetch Vapi call: {response.status_code}")
        print(f"Response: {response.text}")
        return None

    except requests.exceptions.Timeout:
        print(f"Error: Request timed out after {API_TIMEOUT} seconds")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Vapi call: {e}")
        return None


def transform_vapi_transcript(vapi_messages):
    """Transform Vapi messages to Coval transcript format.

    CRITICAL: Vapi uses reversed role naming.
    - Vapi "user" role = your AI assistant -> maps to "assistant" in Coval
    - Vapi "bot" role = end user/customer -> maps to "user" in Coval

    Args:
        vapi_messages: List of Vapi message dictionaries

    Returns:
        List of transformed messages in Coval format
    """
    print("\nTransforming Vapi transcript...")

    transcript = []

    for idx, msg in enumerate(vapi_messages):
        vapi_role = msg.get("role", "")
        message_text = msg.get("message", "")

        # Role reversal mapping
        if vapi_role == "user":
            coval_role = "assistant"
        elif vapi_role == "bot":
            coval_role = "user"
        elif vapi_role == "system":
            coval_role = "system"
        else:
            print(f"Unknown role '{vapi_role}' in message {idx}, skipping")
            continue

        start_time = msg.get("secondsFromStart", 0.0)
        duration_ms = msg.get("duration", 0)
        duration_seconds = duration_ms / 1000.0
        end_time = start_time + duration_seconds

        entry = {
            "role": coval_role,
            "content": message_text,
            "start_time": start_time,
            "end_time": end_time,
        }

        if "time" in msg:
            entry["timestamp"] = msg["time"]

        transcript.append(entry)
        print(f"[{idx+1}] {vapi_role} -> {coval_role}: {message_text[:50]}...")

    print(f"Transformed {len(transcript)} messages")
    return transcript


def upload_to_coval(call_data, transcript):
    """Upload conversation to Coval for evaluation.

    Args:
        call_data: Dictionary containing Vapi call details
        transcript: List of transformed message dictionaries

    Returns:
        API response dictionary or None if error
    """
    print("\nUploading to Coval...")

    audio_url = call_data.get("stereoRecordingUrl") or call_data.get("recordingUrl")

    payload = {
        "transcript": transcript,
        "external_conversation_id": call_data.get("id"),
        "occurred_at": (
            call_data.get("startedAt")
            or call_data.get("createdAt")
            or datetime.utcnow().isoformat()
        ),
        "metadata": {
            "platform": "vapi",
            "vapi_call_id": call_data.get("id"),
            "status": call_data.get("status"),
            "type": call_data.get("type"),
            "assistant_id": call_data.get("assistantId"),
        },
    }

    if audio_url:
        payload["audio_url"] = audio_url
        print(f"Audio URL: {audio_url[:60]}...")
        print(f"Transcript: {len(transcript)} messages with timing")

    if "customer" in call_data:
        payload["metadata"]["customer_number"] = call_data["customer"].get("number")

    if "endedReason" in call_data:
        payload["metadata"]["ended_reason"] = call_data["endedReason"]

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
    if VAPI_API_KEY == "your_vapi_api_key":
        print("Error: Please update VAPI_API_KEY in the configuration section")
        sys.exit(1)

    if COVAL_API_KEY == "your_coval_api_key":
        print("Error: Please update COVAL_API_KEY in the configuration section")
        sys.exit(1)

    if CALL_ID == "your_vapi_call_id":
        print("Error: Please update CALL_ID in the configuration section")
        sys.exit(1)

    call_data = get_vapi_call(CALL_ID)
    if not call_data:
        sys.exit(1)

    vapi_messages = call_data.get("messages", [])
    if not vapi_messages:
        print("Warning: No messages found in Vapi call")
        transcript = []
    else:
        transcript = transform_vapi_transcript(vapi_messages)

    result = upload_to_coval(call_data, transcript)

    if not result:
        print("Upload failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
