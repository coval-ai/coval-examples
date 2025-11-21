#!/usr/bin/env python3
"""
Launch Run Example for Coval

Requirements:
    pip install requests

Documentation:
    - Runs API: https://docs.coval.dev/api-reference/v1/runs
"""

import sys
import time

import requests

# Configuration
COVAL_API_KEY = "your_coval_api_key"
AGENT_ID = "your_agent_id"
PERSONA_ID = "your_persona_id"
TEST_SET_ID = "your_test_set_id"
METRIC_IDS = []
ITERATION_COUNT = 1
CONCURRENCY = 1

# Constants
API_TIMEOUT = 30
RUNS_API_URL = "https://api.coval.dev/v1/runs"
POLL_INTERVAL_SECONDS = 5


def launch_run(
    agent_id, persona_id, test_set_id, metric_ids=None, iteration_count=1, concurrency=1
):
    """Launch a simulation run.

    Args:
        agent_id: Agent to test (22 characters)
        persona_id: Simulated persona (22 characters)
        test_set_id: Test set ID (8 characters)
        metric_ids: Optional list of metric IDs
        iteration_count: Number of iterations per test case (1-10)
        concurrency: Concurrent simulation count (1-5)

    Returns:
        Run ID string or None if error
    """
    print("Launching simulation run...")
    print(f"Agent ID: {agent_id}")
    print(f"Persona ID: {persona_id}")
    print(f"Test Set ID: {test_set_id}")

    payload = {
        "agent_id": agent_id,
        "persona_id": persona_id,
        "test_set_id": test_set_id,
        "options": {"iteration_count": iteration_count, "concurrency": concurrency},
    }

    if metric_ids and len(metric_ids) > 0:
        payload["metric_ids"] = metric_ids

    headers = {"X-API-Key": COVAL_API_KEY, "Content-Type": "application/json"}

    try:
        response = requests.post(
            RUNS_API_URL, json=payload, headers=headers, timeout=API_TIMEOUT
        )

        if response.status_code == 200:
            result = response.json()
            run = result.get("run", {})
            run_id = run.get("run_id")
            status = run.get("status")
            create_time = run.get("create_time")

            print("\nRun launched successfully")
            print(f"Run ID: {run_id}")
            print(f"Status: {status}")
            print(f"Created: {create_time}")
            print(f"View at: https://app.coval.dev/runs/{run_id}")

            return run_id

        print(f"\nFailed to launch run: {response.status_code}")
        print(f"Response: {response.text}")
        return None

    except requests.exceptions.Timeout:
        print(f"\nError: Request timed out after {API_TIMEOUT} seconds")
        return None
    except requests.exceptions.RequestException as e:
        print(f"\nError launching run: {e}")
        return None


def get_run_status(run_id):
    """Get the current status of a run.

    Args:
        run_id: Run ID to query

    Returns:
        Status string or None if error
    """
    headers = {"X-API-Key": COVAL_API_KEY, "Content-Type": "application/json"}

    try:
        response = requests.get(
            f"{RUNS_API_URL}/{run_id}", headers=headers, timeout=API_TIMEOUT
        )

        if response.status_code == 200:
            result = response.json()
            run = result.get("run", {})
            status = run.get("status")
            return status

        print(f"Failed to get run status: {response.status_code}")
        return None

    except requests.exceptions.Timeout:
        print(f"Error: Request timed out after {API_TIMEOUT} seconds")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error getting run status: {e}")
        return None


def main():
    """Main execution function."""
    if COVAL_API_KEY == "your_coval_api_key":
        print("Error: Please update COVAL_API_KEY")
        sys.exit(1)

    if AGENT_ID == "your_agent_id":
        print("Error: Please update AGENT_ID")
        sys.exit(1)

    if PERSONA_ID == "your_persona_id":
        print("Error: Please update PERSONA_ID")
        sys.exit(1)

    if TEST_SET_ID == "your_test_set_id":
        print("Error: Please update TEST_SET_ID")
        sys.exit(1)

    run_id = launch_run(
        agent_id=AGENT_ID,
        persona_id=PERSONA_ID,
        test_set_id=TEST_SET_ID,
        metric_ids=METRIC_IDS,
        iteration_count=ITERATION_COUNT,
        concurrency=CONCURRENCY,
    )

    if not run_id:
        print("Failed to launch run")
        sys.exit(1)

    print("\nMonitoring run status...")
    while True:
        status = get_run_status(run_id)
        if not status:
            break

        print(f"Current status: {status}")

        if status in ["COMPLETED", "FAILED", "CANCELLED"]:
            print(f"\nRun finished with status: {status}")
            break

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
