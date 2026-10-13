#!/usr/bin/env python3
"""Refuse Vertex dispatch unless a fresh response identifies the requested model.

Only model identity, HTTP status, usage and nonsecret probe settings are retained.
Credentials, project names and response text never enter the receipt or diagnostics.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import google.auth
from google.auth.transport.requests import Request
from google.oauth2 import service_account
import requests


def atomic_write(path: Path, record: dict[str, Any]) -> None:
    """Publish a complete receipt atomically; refuse any failed write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(record, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def probe_identity(model: str, location: str, credential_mode: str, out: Path) -> int:
    """Refuse auth/network errors, non-200 responses and absent or different model identity."""
    try:
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        if credential_mode == "repo-dotenv":
            credentials = service_account.Credentials.from_service_account_file(
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=scopes,
            )
        elif credential_mode == "opencode":
            credentials, _ = google.auth.default(scopes=scopes)
        else:
            raise ValueError("unsupported credential mode")
        project = os.environ["GOOGLE_VERTEX_PROJECT"]
        if not project or not model or not location:
            raise ValueError("missing probe configuration")
        credentials.refresh(Request())
        response = requests.post(
            f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}"
            f"/publishers/google/models/{model}:generateContent",
            headers={"Authorization": f"Bearer {credentials.token}"},
            json={
                "contents": [{"role": "user", "parts": [{"text": "Reply with exactly one word: ok"}]}],
                "generationConfig": {"maxOutputTokens": 64, "thinkingConfig": {"thinkingLevel": "low"}},
            },
            timeout=120,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("response must be an object")
        served = payload.get("modelVersion")
        record = {
            "requested_model": model,
            "served_model_version": served,
            "http_status": response.status_code,
            "usage": payload.get("usageMetadata"),
            "checked_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
            "credential_mode": credential_mode,
            "location": location,
        }
        atomic_write(out, record)
        if response.status_code == 200 and served == model:
            return 0
        print(f"vertex-identity-probe: status={response.status_code} served={json.dumps(served)}",
              file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"vertex-identity-probe: {type(exc).__name__}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Refuse missing CLI arguments and credential modes outside the two supported routes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--credential-mode", required=True, choices=["repo-dotenv", "opencode"])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    return probe_identity(args.model, args.location, args.credential_mode, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
