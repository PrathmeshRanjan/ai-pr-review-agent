#!/usr/bin/env python3
"""
scripts/trigger-pr-review.py — Trigger an AI review for a real GitHub Pull Request.

Usage:
  python3 scripts/trigger-pr-review.py <owner/repo> <pr_number>

Examples:
  # From host machine:
  python3 scripts/trigger-pr-review.py PrathmeshRanjan/ai-pr-review-agent 1

  # Or inside Docker:
  docker compose -f docker-compose.dev.yml exec api python3 scripts/trigger-pr-review.py PrathmeshRanjan/ai-pr-review-agent 1
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("trigger-pr-review")


async def main():
    parser = argparse.ArgumentParser(
        description="Trigger an AI PR Review for an active GitHub Pull Request."
    )
    parser.add_argument("repo", type=str, help="Repository in 'owner/repo' format")
    parser.add_argument("pr", type=int, help="Pull Request number")
    parser.add_argument(
        "--api-base",
        type=str,
        default=None,
        help="API Base URL (defaults to http://localhost:8001 or internal http://localhost:8000)",
    )
    args = parser.parse_args()

    repo = args.repo.strip()
    pr_number = args.pr
    cfg = get_settings()

    # Determine default API base URL
    if args.api_base:
        api_base = args.api_base.rstrip("/")
    else:
        # Check if running inside container or on host
        api_base = "http://localhost:8000" if os.path.exists("/.dockerenv") else f"http://localhost:{cfg.api_port or 8001}"

    print("\n" + "=" * 65)
    print(f"  AI PR Review Agent — Triggering Review for Real PR")
    print(f"  Target Repository: {repo}")
    print(f"  Pull Request:      #{pr_number}")
    print(f"  Target API:        {api_base}")
    print("=" * 65 + "\n")

    if not cfg.github_token:
        print("[Error] GITHUB_TOKEN is not set in .env. Cannot fetch PR from GitHub.\n")
        sys.exit(1)

    # 1. Fetch real PR details from GitHub API
    logger.info("Fetching PR #%d metadata from GitHub API...", pr_number)
    github_headers = {
        "Authorization": f"Bearer {cfg.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-pr-review-agent/1.0",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
            headers=github_headers,
        )
        if resp.status_code == 404:
            print(f"\n[Error] PR #{pr_number} not found on repository '{repo}'.")
            print("Make sure the PR is opened on GitHub and your GITHUB_TOKEN has access.\n")
            sys.exit(1)
        resp.raise_for_status()
        pr_data = resp.json()

    head_sha = pr_data["head"]["sha"]
    title = pr_data.get("title", "")
    author = pr_data.get("user", {}).get("login", "unknown")
    state = pr_data.get("state", "open")
    html_url = pr_data.get("html_url", "")

    logger.info("Found PR: '%s' by @%s (state: %s, head: %s)", title, author, state, head_sha[:8])

    # 2. Build GitHub webhook payload
    payload = {
        "action": "opened",
        "number": pr_number,
        "pull_request": pr_data,
        "repository": pr_data.get("base", {}).get("repo", {"full_name": repo}),
        "sender": pr_data.get("user", {"login": author}),
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    # 3. Compute HMAC signature
    secret = (cfg.github_webhook_secret or "").encode("utf-8")
    sig = "sha256=" + hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()

    # 4. POST to local webhook receiver
    logger.info("Sending webhook to %s/webhook/github...", api_base)
    webhook_headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": sig,
        "X-GitHub-Delivery": f"cli-trigger-{int(time.time())}",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            webhook_resp = await client.post(
                f"{api_base}/webhook/github",
                content=payload_bytes,
                headers=webhook_headers,
            )
        except Exception as e:
            print(f"\n[Error] Failed to connect to API at {api_base}: {e}")
            print("Is Docker running? Check: docker compose -f docker-compose.dev.yml ps\n")
            sys.exit(1)

        if webhook_resp.status_code not in (200, 202):
            print(f"\n[Error] Webhook rejected ({webhook_resp.status_code}): {webhook_resp.text}\n")
            sys.exit(1)

        logger.info("Webhook accepted: %s", webhook_resp.json())

    # 5. Poll for review completion
    logger.info("Polling for review verdict (this may take 15-30s while agents run)...")
    expected_workflow_id = f"{repo}:{pr_number}:{head_sha}"
    api_key_header = {"X-API-Key": cfg.api_key or "change-me-in-production"}

    max_poll_seconds = 120
    poll_start = time.time()

    async with httpx.AsyncClient(timeout=10.0) as client:
        while time.time() - poll_start < max_poll_seconds:
            await asyncio.sleep(2)
            try:
                rev_resp = await client.get(
                    f"{api_base}/api/v1/reviews?limit=10",
                    headers=api_key_header,
                )
                if rev_resp.status_code == 200:
                    data = rev_resp.json()
                    items = data.get("items", [])
                    matching = [r for r in items if r.get("id") == expected_workflow_id]
                    if matching:
                        review = matching[0]
                        print("\n" + "=" * 65)
                        print("  PR Review Result")
                        print("=" * 65)
                        print(f"  Status:             {review.get('status', '').upper()}")
                        print(f"  Verdict:            {review.get('verdict', '').upper()}")
                        print(f"  Overall Confidence: {review.get('overall_confidence', 0):.2f}")
                        print(f"  Findings Count:     {review.get('finding_count', 0)}")
                        print(f"  Human Review Req:   {review.get('needs_human_review', False)}")
                        if review.get("human_review_reason"):
                            print(f"  Escalation Reason:  {review.get('human_review_reason')}")
                        print("=" * 65)
                        print(f"\nGitHub PR URL: {html_url}")
                        return
            except Exception as e:
                logger.debug("Poll check: %s", e)

    print("\n[Warning] Review is still processing. Check the logs or query /api/v1/reviews.")


if __name__ == "__main__":
    asyncio.run(main())

