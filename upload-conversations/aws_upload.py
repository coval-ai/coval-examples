#!/usr/bin/env python3
"""
AWS S3 Presigned URL Upload Example for Coval

This script demonstrates how to:
1. Generate presigned URLs for audio files in AWS S3
2. Upload conversations to Coval using S3 presigned URLs

Requirements:
    pip install boto3 requests

Authentication:
    aws configure
    OR set environment variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY

Documentation:
    - AWS S3 Presigned URLs: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/s3-presigned-urls.html
    - Coval API: https://docs.coval.dev/api-reference/v1/conversations/submit-conversation-for-evaluation
"""

import sys
from datetime import datetime

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError

# Configuration
COVAL_API_KEY = "your_coval_api_key"
S3_BUCKET_NAME = "your-bucket-name"
S3_OBJECT_KEY = "path/to/audio.wav"
AWS_REGION = "us-east-1"
EXTERNAL_CONVERSATION_ID = "aws-conversation-123"
OCCURRED_AT = None
METRIC_IDS = []

# Constants
API_TIMEOUT = 30
COVAL_API_URL = "https://api.coval.dev/v1/conversations:submit"
MAX_FILE_SIZE_MB = 200


def generate_presigned_url(bucket_name, object_key, region=None, expiration=3600):
    """Generate a presigned URL for an S3 object.

    Args:
        bucket_name: S3 bucket name
        object_key: S3 object key (path)
        region: AWS region
        expiration: Seconds until URL expires

    Returns:
        Presigned URL string or None if error
    """
    print(f"Generating presigned URL for s3://{bucket_name}/{object_key}")

    try:
        config = Config(signature_version="s3v4", region_name=region)
        s3_client = boto3.client("s3", config=config)

        try:
            response = s3_client.head_object(Bucket=bucket_name, Key=object_key)
            content_length = response["ContentLength"]
            content_type = response.get("ContentType", "unknown")

            print(f"File size: {content_length / (1024*1024):.2f} MB")
            print(f"Content type: {content_type}")

            if content_length > MAX_FILE_SIZE_MB * 1024 * 1024:
                print(
                    f"Error: File too large ({content_length / (1024*1024):.2f} MB). "
                    f"Maximum is {MAX_FILE_SIZE_MB} MB."
                )
                return None

        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                print(f"Error: Object not found: s3://{bucket_name}/{object_key}")
            else:
                print(f"Error checking object: {e}")
            return None

        presigned_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": object_key},
            ExpiresIn=expiration,
        )

        print("Presigned URL generated successfully")
        print(f"Expires in: {expiration} seconds ({expiration/3600:.1f} hours)")

        return presigned_url

    except ClientError as e:
        print(f"AWS Error: {e}")
        return None
    except Exception as e:
        print(f"Error generating presigned URL: {e}")
        return None


def upload_to_coval(audio_url, external_conversation_id=None, occurred_at=None):
    """Upload conversation to Coval for evaluation.

    Args:
        audio_url: S3 presigned URL
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
        "metadata": {"platform": "aws", "bucket": S3_BUCKET_NAME, "source": "s3"},
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

    if S3_BUCKET_NAME == "your-bucket-name":
        print("Error: Please update S3_BUCKET_NAME")
        sys.exit(1)

    if S3_OBJECT_KEY == "path/to/audio.wav":
        print("Error: Please update S3_OBJECT_KEY")
        sys.exit(1)

    presigned_url = generate_presigned_url(
        S3_BUCKET_NAME, S3_OBJECT_KEY, region=AWS_REGION, expiration=3600
    )

    if not presigned_url:
        sys.exit(1)

    result = upload_to_coval(
        audio_url=presigned_url,
        external_conversation_id=EXTERNAL_CONVERSATION_ID,
        occurred_at=OCCURRED_AT,
    )

    if not result:
        print("Upload failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
