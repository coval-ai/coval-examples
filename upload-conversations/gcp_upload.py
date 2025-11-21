#!/usr/bin/env python3
"""
Google Cloud Storage Signed URL Upload Example for Coval

This script demonstrates how to:
1. Generate signed URLs for audio files in Google Cloud Storage
2. Upload conversations to Coval using GCP signed URLs

Requirements:
    pip install google-cloud-storage requests

Authentication:
    export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"

Documentation:
    - GCP Signed URLs: https://cloud.google.com/storage/docs/access-control/signed-urls
    - Coval API: https://docs.coval.dev/api-reference/v1/conversations/submit-conversation-for-evaluation
"""

import sys
from datetime import datetime, timedelta

import requests
from google.cloud import storage

# Configuration
COVAL_API_KEY = "your_coval_api_key"
GCS_BUCKET_NAME = "your-bucket-name"
GCS_BLOB_PATH = "path/to/audio.wav"
EXTERNAL_CONVERSATION_ID = "gcp-conversation-123"
OCCURRED_AT = None
METRIC_IDS = []

# Constants
API_TIMEOUT = 30
COVAL_API_URL = "https://api.coval.dev/v1/conversations:submit"


def generate_signed_url(bucket_name, blob_path, expiration_hours=1):
    """Generate a v4 signed URL for a Google Cloud Storage object.

    Args:
        bucket_name: GCS bucket name
        blob_path: Path to object in bucket
        expiration_hours: Hours until URL expires

    Returns:
        Signed URL string or None if error
    """
    print(f"Generating signed URL for gs://{bucket_name}/{blob_path}")

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_path)

        if not blob.exists():
            print(f"Error: Blob does not exist: gs://{bucket_name}/{blob_path}")
            return None

        blob.reload()
        print(f"File size: {blob.size / (1024*1024):.2f} MB")
        print(f"Content type: {blob.content_type}")

        expiration = timedelta(hours=expiration_hours)
        signed_url = blob.generate_signed_url(
            version="v4", expiration=expiration, method="GET"
        )

        print("Signed URL generated successfully")
        print(f"Expires in: {expiration_hours} hour(s)")

        return signed_url

    except Exception as e:
        print(f"Error generating signed URL: {e}")
        return None


def upload_to_coval(audio_url, external_conversation_id=None, occurred_at=None):
    """Upload conversation to Coval for evaluation.

    Args:
        audio_url: GCS signed URL
        external_conversation_id: Optional external ID
        occurred_at: Optional ISO timestamp

    Returns:
        API response dictionary or None if error
    """
    print("\nUploading to Coval...")

    payload = {
        "audio_url": audio_url,
        "external_conversation_id": external_conversation_id,
        "occurred_at": occurred_at or datetime.utcnow().isoformat() + "Z",
        "metadata": {
            "platform": "gcp",
            "bucket": GCS_BUCKET_NAME,
            "source": "cloud-storage",
        },
    }

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
    if COVAL_API_KEY == "your_coval_api_key":
        print("Error: Please update COVAL_API_KEY")
        sys.exit(1)

    if GCS_BUCKET_NAME == "your-bucket-name":
        print("Error: Please update GCS_BUCKET_NAME")
        sys.exit(1)

    if GCS_BLOB_PATH == "path/to/audio.wav":
        print("Error: Please update GCS_BLOB_PATH")
        sys.exit(1)

    signed_url = generate_signed_url(
        GCS_BUCKET_NAME, GCS_BLOB_PATH, expiration_hours=1
    )

    if not signed_url:
        sys.exit(1)

    result = upload_to_coval(
        audio_url=signed_url,
        external_conversation_id=EXTERNAL_CONVERSATION_ID,
        occurred_at=OCCURRED_AT,
    )

    if not result:
        print("Upload failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
