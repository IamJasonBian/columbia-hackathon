#!/usr/bin/env python3
"""Mint a local backend ID token and print export lines for .env."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.token_provider import TokenProviderError, backend_token_provider  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint Google SA ID token for local CRUD services")
    parser.add_argument(
        "--audience",
        default=os.environ.get("BACKEND_TOKEN_AUDIENCE") or os.environ.get("BACKEND_URL"),
        help="Token audience (default: BACKEND_TOKEN_AUDIENCE or BACKEND_URL)",
    )
    parser.add_argument(
        "--write-env",
        type=Path,
        default=None,
        help="Append LOCAL_SA_TOKEN=... to this file (default: skip)",
    )
    args = parser.parse_args()

    if args.audience:
        os.environ.setdefault("BACKEND_TOKEN_AUDIENCE", args.audience)

    try:
        token = backend_token_provider().get_token(force_refresh=True)
    except TokenProviderError as exc:
        print(f"mint failed: {exc}", file=sys.stderr)
        print(
            "\nSet one of:\n"
            "  BACKEND_SA_FILE=/path/to/key.json\n"
            "  BACKEND_IMPERSONATE_SERVICE_ACCOUNT=sa@project.iam.gserviceaccount.com\n"
            "  (on GCP) metadata server\n",
            file=sys.stderr,
        )
        return 1

    print("# paste into shell or .env")
    print(f"export LOCAL_SA_TOKEN='{token}'")
    print("export SKIP_AUTH=0")
    print(f"export BACKEND_TOKEN_AUDIENCE='{args.audience or os.environ.get('BACKEND_TOKEN_AUDIENCE', '')}'")

    if args.write_env:
        line = f"LOCAL_SA_TOKEN={token}\n"
        with args.write_env.open("a") as f:
            f.write(line)
        print(f"\nappended LOCAL_SA_TOKEN to {args.write_env}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
