#!/usr/bin/env python3
"""
scripts/index-repo.py — Pre-index repository source files into vector memory.

Usage:
  python3 scripts/index-repo.py <owner/repo>

Example:
  python3 scripts/index-repo.py octocat/Hello-World

What this script does:
  1. Loads configuration from .env
  2. Verifies GitHub and Google API credentials
  3. Initializes the database and vector schema (code_chunks table + pgvector DiskANN)
  4. Scans the target GitHub repository for source code files
  5. Computes vector embeddings using Google's gemini-embedding-001 model
  6. Stores chunks in PostgreSQL so the very first PR review already has RAG context
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("index-repo")


async def main():
    parser = argparse.ArgumentParser(
        description="Pre-index a GitHub repository into vector memory for PR review RAG."
    )
    parser.add_argument(
        "repo",
        type=str,
        help="Full repository name in 'owner/repo' format (e.g. octocat/Hello-World)",
    )
    args = parser.parse_args()
    repo_full_name = args.repo.strip()

    if "/" not in repo_full_name or len(repo_full_name.split("/")) != 2:
        print(f"\n[Error] Invalid repository format: '{repo_full_name}'. Expected 'owner/repo'.\n")
        sys.exit(1)

    from backend.config.settings import get_settings
    cfg = get_settings()

    print("\n" + "=" * 60)
    print(f"  AI PR Review Agent — Repository Indexing Tool")
    print(f"  Target Repository: {repo_full_name}")
    print(f"  Embedding Model:   {cfg.google_embedding_model} ({cfg.google_embedding_dimensions} dims)")
    print("=" * 60 + "\n")

    # Check required credentials
    if not cfg.github_token:
        print("[Error] GITHUB_TOKEN is not set in .env. Cannot access GitHub repository.\n")
        sys.exit(1)

    if not cfg.google_api_key:
        print("[Error] GOOGLE_API_KEY is not set in .env. Cannot generate embeddings.\n")
        sys.exit(1)

    logger.info("Initializing database connections and vector schema...")
    from backend.database.postgres import init_db, init_tiger_schema
    try:
        await init_db()
        await init_tiger_schema()
    except Exception as e:
        logger.error("Failed to connect to database: %s", e)
        print("\n[Error] Database initialization failed. Check DATABASE_URL in .env.")
        sys.exit(1)

    logger.info("Starting ingestion for %s...", repo_full_name)
    from backend.data.ingestion import ingest_repository
    try:
        summary = await ingest_repository(repo_full_name)
    except Exception as e:
        logger.error("Ingestion failed with exception: %s", e)
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  Repository Indexing Summary")
    print("=" * 60)
    print(f"  Total code files found:   {summary.get('total_files', 0)}")
    print(f"  Files requiring indexing: {summary.get('stale_files', 0)}")
    print(f"  Successfully embedded:    {summary.get('embedded', 0)}")
    print(f"  Skipped (already fresh):  {summary.get('skipped_fresh', 0)}")
    print(f"  Errors / Fetch failures:  {summary.get('errors', 0)}")
    print("=" * 60 + "\n")

    if summary.get("embedded", 0) > 0 or summary.get("skipped_fresh", 0) > 0:
        print("Done! Code chunks and embeddings are stored in PostgreSQL.")
        print("Subsequent PR reviews on this repository will now use full RAG context.\n")
    else:
        print("Notice: No files were embedded. Check repository name and token permissions.\n")


if __name__ == "__main__":
    asyncio.run(main())

