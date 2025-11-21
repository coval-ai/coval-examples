#!/usr/bin/env python3
"""
Generic Signed URL Upload Example for Coval

This script demonstrates how to upload conversations to Coval using
signed/presigned URLs from any platform or cloud storage provider.

Supported URL types:
- AWS S3 presigned URLs
- Google Cloud Storage signed URLs
- Azure Blob Storage SAS URLs
- Custom CDN signed URLs
- Any publicly accessible HTTPS URL

Requirements:
    pip install requests

Documentation:
    - Coval API: https://docs.coval.dev/api-reference/v1/conversations/submit-conversation-for-evaluation
"""

import sys
from datetime import datetime

import requests

# Configuration
COVAL_API_KEY = "your_coval_api_key"
AUDIO_SIGNED_URL = (
    "https://your-platform.com/recordings/conversation.wav?signature=abc123"
)
TRANSCRIPT = None
EXTERNAL_CONVERSATION_ID = "your-platform-conversation-id-123"
OCCURRED_AT = None
METRIC_IDS = []
METADATA = {
    "platform": "your-platform-name",
    "channel": "voice",
}

# Constants
API_TIMEOUT = 30
COVAL_API_URL = "https://api.coval.dev/v1/conversations:submit"
MAX_FILE_SIZE_MB = 200


def validate_audio_url(url):
    """Validate that the audio URL is accessible.

    Args:
        url: URL to validate

    Returns:
        True if valid and accessible, False otherwise
    """
    print("Validating audio URL...")

    if not url.startswith("https://"):
        print("Error: URL must use HTTPS protocol")
        return False

    try:
        response = requests.head(url, timeout=10, allow_redirects=True)

        if response.status_code == 200:
            content_length = response.headers.get("Content-Length")
            if content_length:
                size_mb = int(content_length) / (1024 * 1024)
                print(f"File size: {size_mb:.2f} MB")

                if size_mb > MAX_FILE_SIZE_MB:
                    print(
                        f"Error: File too large ({size_mb:.2f} MB). "
                        f"Maximum is {MAX_FILE_SIZE_MB} MB."
                    )
                    return False

            content_type = response.headers.get("Content-Type", "unknown")
            print(f"Content type: {content_type}")
            print("URL is accessible")
            return True

        print(f"Error: URL returned status code {response.status_code}")
        return False

    except requests.exceptions.Timeout:
        print("Warning: URL validation timed out")
        return True
    except requests.exceptions.RequestException as e:
        print(f"Warning: Could not validate URL: {e}")
        return True


def upload_to_coval(
    audio_url,
    transcript=None,
    external_conversation_id=None,
    occurred_at=None,
    metadata=None,
    metrics=None,
):
    """Upload conversation to Coval for evaluation.

    Args:
        audio_url: Signed URL to audio file
        transcript: Optional pre-existing transcript
        external_conversation_id: Optional external ID
        occurred_at: Optional ISO timestamp
        metadata: Optional metadata dictionary
        metrics: Optional list of metric IDs

    Returns:
        API response dictionary or None if error
    """
    print("\nUploading to Coval...")

    payload = {"audio_url": audio_url}

    if transcript and len(transcript) > 0:
        payload["transcript"] = transcript
        print(f"Transcript: {len(transcript)} messages with timing")
        print(f"Audio URL: {audio_url[:60]}...")
    else:
        print(f"Audio only: {audio_url[:60]}...")

    if external_conversation_id:
        payload["external_conversation_id"] = external_conversation_id

    payload["occurred_at"] = occurred_at or datetime.utcnow().isoformat() + "Z"

    if metadata:
        payload["metadata"] = metadata

    if metrics and len(metrics) > 0:
        payload["metrics"] = metrics

    headers = {"X-API-Key": COVAL_API_KEY, "Content-Type": "application/json"}

    try:
        response = requests.post(
            COVAL_API_URL, json=payload, headers=headers, timeout=API_TIMEOUT
        )

        if response.status_code == 200:
            result = response.json()
            conversation = result.get("conversation", {})
            conversation_id = conversation.get("conversation_id")
            status = conversation.get("status")

            print("\nSuccessfully uploaded to Coval")
            print(f"Conversation ID: {conversation_id}")
            print(f"Status: {status}")
            print(f"View at: https://app.coval.dev/conversations/{conversation_id}")

            return result

        print(f"\nFailed to upload to Coval: {response.status_code}")
        print(f"Response: {response.text}")
        return None

    except requests.exceptions.Timeout:
        print(f"\nError: Request timed out after {API_TIMEOUT} seconds")
        return None
    except requests.exceptions.RequestException as e:
        print(f"\nError uploading to Coval: {e}")
        return None


def main():
    """Main execution function."""
    if COVAL_API_KEY == "your_coval_api_key":
        print("Error: Please update COVAL_API_KEY")
        sys.exit(1)

    if AUDIO_SIGNED_URL.startswith("https://your-platform.com"):
        print("Error: Please update AUDIO_SIGNED_URL")
        sys.exit(1)

    if not validate_audio_url(AUDIO_SIGNED_URL):
        print("\nWarning: URL validation failed, continuing with upload attempt...")

    result = upload_to_coval(
        audio_url=AUDIO_SIGNED_URL,
        transcript=TRANSCRIPT,
        external_conversation_id=EXTERNAL_CONVERSATION_ID,
        occurred_at=OCCURRED_AT,
        metadata=METADATA,
        metrics=METRIC_IDS,
    )

    if not result:
        print("Upload failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
