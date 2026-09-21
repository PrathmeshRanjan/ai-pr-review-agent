#!/usr/bin/env python3
"""
scripts/run-action-review.py — Serverless GitHub Actions PR Review Runner.

This script executes the AI PR Review Agent directly inside a GitHub Actions CI
environment without requiring a 24/7 web server, public tunnel, or webhook receiver.

How it works in GitHub Actions:
  1. Triggered on pull_request events ([opened, synchronize, reopened]).
  2. Parses GitHub Actions environment ($GITHUB_EVENT_PATH, $GITHUB_TOKEN, $GITHUB_REPOSITORY).
  3. Evaluates the PR diff with pre-flight security threat modeling (prompt injection, secrets, PII).
  4. Runs the multi-agent specialist review pipeline (Security, Quality, Test, Docs).
  5. Aggregates findings and posts the review comment onto the Pull Request via GitHub REST API.
  6. Emits a rich Markdown summary to $GITHUB_STEP_SUMMARY for native GitHub Actions UI visualization.

Local / Dry-run usage:
  # Dry-run with sample PR payload fixture:
  python3 scripts/run-action-review.py --event-path fixtures/sample_pr_opened.json --dry-run

  # Run against a live repo and PR:
  python3 scripts/run-action-review.py --repo owner/repo --pr 42 --token $GITHUB_TOKEN
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

# Ensure repository root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Safe defaults for non-server environment
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "gha-serverless-runner")
if os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gha-review-runner")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run AI PR Review Agent directly in GitHub Actions or CLI."
    )
    parser.add_argument(
        "--event-path",
        type=str,
        default=os.environ.get("GITHUB_EVENT_PATH"),
        help="Path to GitHub event JSON (defaults to $GITHUB_EVENT_PATH)",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="Repository in 'owner/repo' format (defaults to $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="Pull request number (defaults to PR number from event payload)",
    )
    parser.add_argument(
        "--sha",
        type=str,
        default=None,
        help="Head commit SHA (defaults to PR head SHA from event payload)",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub API token (defaults to $GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run analysis without posting comments/reviews to GitHub API",
    )
    parser.add_argument(
        "--fail-on-changes",
        action="store_true",
        help="Exit with non-zero status if review verdict requests changes or flags critical issues",
    )
    return parser.parse_args()


def load_event_data(event_path: str | None) -> dict[str, Any]:
    """Loads event JSON if path is provided and exists."""
    if not event_path:
        return {}
    path = Path(event_path)
    if not path.is_file():
        logger.warning("Event file not found at: %s", event_path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to parse event JSON at %s: %s", event_path, e)
        return {}


async def run_review(args: argparse.Namespace) -> int:
    event = load_event_data(args.event_path)

    # 1. Resolve PR metadata
    pr_data = event.get("pull_request") or {}
    repo_full_name = (
        args.repo
        or (event.get("repository", {}).get("full_name") if isinstance(event.get("repository"), dict) else None)
    )
    pr_number = args.pr or event.get("number") or pr_data.get("number")
    head_sha = (
        args.sha
        or pr_data.get("head", {}).get("sha")
        or event.get("after")
    )
    pr_title = pr_data.get("title", f"PR #{pr_number}")
    pr_body = pr_data.get("body", "")
    author_login = pr_data.get("user", {}).get("login", "unknown")
    base_branch = pr_data.get("base", {}).get("ref", "main")
    pr_diff = pr_data.get("diff", "")

    if not repo_full_name:
        print("\n[Error] Repository name not provided. Use --repo or set GITHUB_REPOSITORY.\n")
        return 1
    if not pr_number:
        print("\n[Error] Pull Request number not provided. Use --pr or specify a valid event payload.\n")
        return 1

    # Apply token to environment if provided
    if args.token:
        os.environ["GITHUB_TOKEN"] = args.token

    from backend.config.settings import get_settings
    get_settings.cache_clear()
    cfg = get_settings()

    print("\n" + "=" * 65)
    print("  AI PR Review Agent — Serverless GitHub Actions Runner")
    print(f"  Repository:    {repo_full_name}")
    print(f"  Pull Request:  #{pr_number}")
    print(f"  Head SHA:      {head_sha or 'auto-detect'}")
    print(f"  Dry Run:       {args.dry_run}")
    print("=" * 65 + "\n")

    workflow_id = f"gha-{pr_number}-{int(time.time())}"

    input_data = {
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
        "pr_title": pr_title,
        "pr_body": pr_body,
        "author_login": author_login,
        "head_commit_sha": head_sha or "0000000000000000000000000000000000000000",
        "base_branch": base_branch,
        "pr_diff": pr_diff,
    }

    from backend.orchestrator.langgraph_engine import LangGraphEngine
    from backend.integrations.github_client import GitHubClient

    engine = LangGraphEngine()

    posted_payloads = []

    if args.dry_run:
        # Mock GitHubClient.post_pr_review in dry-run mode
        original_post = GitHubClient.post_pr_review

        async def dry_run_post(self, repo_full_name, pr_number, payload):
            posted_payloads.append(payload)
            mock_resp = AsyncMock()
            mock_resp.id = 999999
            mock_resp.html_url = f"https://github.com/{repo_full_name}/pull/{pr_number}#dry-run"
            return mock_resp

        with patch.object(GitHubClient, "post_pr_review", dry_run_post):
            result = await engine.run(workflow_id, input_data)
    else:
        result = await engine.run(workflow_id, input_data)

    print("\n" + "-" * 65)
    print(f"  Review Completed: {result.status.value.upper()}")
    print(f"  Verdict:          {result.verdict.value if result.verdict else 'UNKNOWN'}")
    print(f"  Total Findings:   {len(result.findings)}")
    print(f"  Agents Completed: {result.agents_completed} / {result.agents_completed + result.agents_failed}")
    if result.metadata.get("needs_human_review"):
        print(f"  HITL Escalation:  YES ({result.metadata.get('human_review_reason')})")
    print("-" * 65 + "\n")

    # Display findings breakdown
    severity_counts: dict[str, int] = {}
    for f in result.findings:
        sev = f.severity.value
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    for sev in ["critical", "high", "medium", "low"]:
        if sev in severity_counts:
            print(f"  - {sev.upper():8s}: {severity_counts[sev]}")

    # Extract review markdown for output & step summary
    review_markdown = ""
    if posted_payloads:
        review_markdown = posted_payloads[0].body
    elif result.metadata.get("github_review_id"):
        review_markdown = f"Review successfully posted to PR #{pr_number} (Review ID: {result.metadata.get('github_review_id')})."

    # Write to GITHUB_STEP_SUMMARY if available
    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        try:
            with open(step_summary_path, "a", encoding="utf-8") as summary_file:
                summary_file.write(f"\n## AI PR Review Summary (#{pr_number})\n\n")
                summary_file.write(
                    f"**Verdict:** `{result.verdict.value if result.verdict else 'UNKNOWN'}` | "
                    f"**Findings:** `{len(result.findings)}` | "
                    f"**Confidence:** `{result.metadata.get('overall_confidence', 0.0) * 100:.0f}%`\n\n"
                )
                if review_markdown:
                    summary_file.write(review_markdown + "\n")
            logger.info("Published review summary to GitHub Step Summary: %s", step_summary_path)
        except Exception as summary_err:
            logger.warning("Could not write to GITHUB_STEP_SUMMARY: %s", summary_err)

    if args.dry_run and review_markdown:
        print("\n=== GENERATED REVIEW BODY (DRY RUN) ===\n")
        print(review_markdown)
        print("\n=======================================\n")

    # Evaluate exit code
    if args.fail_on_changes:
        from backend.models.enums import ReviewVerdict
        if result.verdict in (ReviewVerdict.REQUEST_CHANGES, ReviewVerdict.NEEDS_HUMAN_REVIEW):
            logger.warning("Failing job because review verdict requires changes or escalation.")
            return 1

    return 0


def main():
    args = parse_args()
    try:
        code = asyncio.run(run_review(args))
        sys.exit(code)
    except KeyboardInterrupt:
        print("\n[Canceled] Execution interrupted by user.")
        sys.exit(130)
    except Exception as exc:
        logger.exception("Review execution failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
