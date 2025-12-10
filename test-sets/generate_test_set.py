#!/usr/bin/env python3
"""
Generate and upload LLM-generated test cases to Coval.

This script uses OpenAI to generate diverse test cases based on a customizable
prompt, then uploads them to Coval as a test set for AI agent evaluation.

Usage:
    export COVAL_API_KEY="your-coval-api-key"
    export OPENAI_API_KEY="your-openai-api-key"
    python generate_test_set.py
"""

import json
import os
import sys

import requests
from openai import OpenAI

COVAL_BASE_URL = "https://api.coval.dev/v1"

TEST_CASE_GENERATION_PROMPT = """
Generate 10 diverse test cases for evaluating a customer support AI agent.

Each test case should be a realistic customer inquiry. Return a JSON array with objects containing:
- "input": the customer's message/question
- "expected_output": a brief description of what a good response should include
- "description": a short label for the test case

Focus on variety: billing questions, technical issues, account management, etc.
"""

TEST_SET_CONFIG = {
    "display_name": "LLM Generated Test Cases",
    "description": "Test cases generated via OpenAI",
    "test_set_type": "SCENARIO",
}


def get_required_env(name: str) -> str:
    """Get a required environment variable or exit with an error."""
    value = os.environ.get(name)
    if not value:
        print(f"Error: {name} environment variable is required", file=sys.stderr)
        sys.exit(1)
    return value


def generate_test_cases(openai_api_key: str) -> list[dict]:
    """Use OpenAI to generate test cases based on the prompt."""
    client = OpenAI(api_key=openai_api_key)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": TEST_CASE_GENERATION_PROMPT}],
        response_format={"type": "json_object"},
    )

    content = json.loads(response.choices[0].message.content)
    return content.get("test_cases", content.get("cases", list(content.values())[0]))


def create_test_set(coval_api_key: str) -> str:
    """Create a test set in Coval and return its ID."""
    response = requests.post(
        f"{COVAL_BASE_URL}/test-sets",
        headers={"X-API-Key": coval_api_key, "Content-Type": "application/json"},
        json=TEST_SET_CONFIG,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["test_set"]["id"]


def create_test_case(coval_api_key: str, test_set_id: str, test_case: dict) -> dict:
    """Create a single test case in the given test set."""
    payload = {
        "test_set_id": test_set_id,
        "input_str": test_case.get("input", test_case.get("input_str", "")),
        "expected_output_str": test_case.get(
            "expected_output", test_case.get("expected_output_str")
        ),
        "description": test_case.get("description"),
    }

    response = requests.post(
        f"{COVAL_BASE_URL}/test-cases",
        headers={"X-API-Key": coval_api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["test_case"]


def main():
    coval_api_key = get_required_env("COVAL_API_KEY")
    openai_api_key = get_required_env("OPENAI_API_KEY")

    print("Generating test cases with OpenAI...")
    test_cases = generate_test_cases(openai_api_key)
    print(f"Generated {len(test_cases)} test cases")

    print("Creating test set...")
    test_set_id = create_test_set(coval_api_key)
    print(f"Created test set: {test_set_id}")

    print("Creating test cases...")
    for i, tc in enumerate(test_cases, 1):
        created = create_test_case(coval_api_key, test_set_id, tc)
        desc = tc.get("description", tc.get("input", "")[:40])
        print(f"  [{i}] {created['id']}: {desc}")

    print(f"\nDone! Test set ID: {test_set_id}")


if __name__ == "__main__":
    main()
