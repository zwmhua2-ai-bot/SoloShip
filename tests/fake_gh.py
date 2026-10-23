#!/usr/bin/env python3
"""Fake `gh` for SoloShip P03 tests. Never touches the network.

Behaviour is driven by env vars:
  FAKE_GH_MODE   ok | noauth | create_fail
  FAKE_GH_STATE  directory used to remember created PRs (for resume tests)
  FAKE_GH_LOG    file that receives one JSON record per invocation
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

MODE = os.environ.get("FAKE_GH_MODE", "ok")
PR_STATE = os.environ.get("FAKE_GH_PR_STATE", "draft")
STATE = Path(os.environ.get("FAKE_GH_STATE", "")) if os.environ.get("FAKE_GH_STATE") else None
LOG = Path(os.environ.get("FAKE_GH_LOG", "")) if os.environ.get("FAKE_GH_LOG") else None


def log(argv: list[str]) -> None:
    if LOG is None:
        return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.time(), "argv": argv}, ensure_ascii=False) + "\n")


def created_pr() -> dict | None:
    if STATE is None:
        return None
    path = STATE / "pr.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    argv = sys.argv[1:]
    log(argv)

    if argv[:1] == ["--version"]:
        print("gh version 2.0.0-fake")
        return 0
    if argv[:2] == ["auth", "status"]:
        if MODE == "noauth":
            sys.stderr.write("You are not logged into any GitHub hosts. Run gh auth login.\n")
            return 1
        print("github.com\n  ✓ Logged in to github.com account fixture-bot (keyring)")
        return 0

    if argv[:2] == ["pr", "list"]:
        pr = created_pr()
        print(json.dumps([pr] if pr else []))
        return 0

    if argv[:1] == ["api"]:
        # 只读 PR 查询（P05-A）；不实现任何写操作
        if MODE == "noauth":
            sys.stderr.write("gh: To use GitHub CLI in a github.com Codespace, please log in\n")
            sys.stderr.write("HTTP 401: Bad credentials (https://api.github.com)\n")
            return 1
        if MODE == "apierror":
            sys.stderr.write("HTTP 404: Not Found (https://api.github.com)\n")
            return 1
        if MODE == "netdown":
            sys.stderr.write("dial tcp: lookup api.github.com: no such host\n")
            sys.stderr.write("could not resolve host\n")
            return 1
        if MODE == "badjson":
            print("not-json-at-all")
            return 0
        payload = {
            "number": 1,
            "state": "open",
            "draft": True,
            "merged": False,
            "merged_at": None,
            "closed_at": None,
            "updated_at": "2026-10-23T13:20:00Z",
        }
        if PR_STATE == "open":
            payload["draft"] = False
        elif PR_STATE == "closed":
            payload.update({"state": "closed", "draft": False,
                            "closed_at": "2026-10-23T13:30:00Z"})
        elif PR_STATE == "merged":
            payload.update({"state": "closed", "draft": False, "merged": True,
                            "merged_at": "2026-10-23T13:30:00Z",
                            "closed_at": "2026-10-23T13:30:00Z"})
        print(json.dumps(payload))
        return 0

    if argv[:2] == ["pr", "create"]:
        if MODE == "create_fail":
            sys.stderr.write("GraphQL: Resource not accessible by integration (createPullRequest)\n")
            return 1
        pr = {"number": 7, "url": "https://github.com/fixture/soloship-demo/pull/7",
              "state": "OPEN", "isDraft": True, "headRefOid": "deadbeef"}
        if STATE is not None:
            STATE.mkdir(parents=True, exist_ok=True)
            (STATE / "pr.json").write_text(json.dumps(pr), encoding="utf-8")
        print(pr["url"])
        return 0

    sys.stderr.write(f"fake gh: unsupported argv {argv}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
