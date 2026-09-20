"""
Minimal GitHub webhook receiver — step 1 of the Copilot-watcher pipeline.

What this does right now:
  1. Verifies the incoming request really came from GitHub (HMAC signature check)
  2. Parses issue_comment / pull_request / pull_request_review_comment events
  3. Filters for activity from the Copilot coding agent bot account
  4. Logs it so you can see, live, when Copilot posts something

What this does NOT do yet (next steps):
  - Classify the comment as DONE / BLOCKED / PROGRESS (step 2)
  - Trigger a Twilio call (step 3)
  - Post your answer back to GitHub (step 4)

Run:
    pip install fastapi uvicorn
    export GITHUB_WEBHOOK_SECRET="the-secret-you-set-in-github"
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload

Then point ngrok at it:
    ngrok http 8000
and put the ngrok URL + "/webhook" as the Payload URL in your GitHub webhook settings.
"""

import hashlib
import hmac
import json
import logging
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("copilot-watcher")

app = FastAPI()

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

# The Copilot coding agent's bot identity on github.com.
# It shows up as login "Copilot" (user id 198982749, type "Bot") on comments/PRs,
# and as "copilot-swe-agent[bot]" on commit authorship.
COPILOT_LOGINS = {"copilot", "copilot-swe-agent[bot]"}


def verify_signature(raw_body: bytes, signature_header: Optional[str]) -> None:
    """Reject anything that isn't validly signed by GitHub with our shared secret."""
    if not WEBHOOK_SECRET:
        # Fine for a first local test, but don't run this publicly reachable without a secret.
        log.warning("No GITHUB_WEBHOOK_SECRET set — skipping signature verification!")
        return
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing/invalid signature header")

    expected = hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="Signature mismatch")


def is_from_copilot(login: Optional[str]) -> bool:
    return (login or "").lower() in COPILOT_LOGINS


@app.post("/webhook")
async def github_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None),
    x_github_event: Optional[str] = Header(default=None),
):
    raw_body = await request.body()
    verify_signature(raw_body, x_hub_signature_256)
    payload = json.loads(raw_body)

    if x_github_event == "issue_comment":
        handle_issue_comment(payload)
    elif x_github_event == "pull_request":
        handle_pull_request(payload)
    elif x_github_event == "pull_request_review_comment":
        handle_review_comment(payload)
    else:
        log.info("Ignoring event type: %s", x_github_event)

    return {"ok": True}


def handle_issue_comment(payload: dict) -> None:
    action = payload.get("action")  # created / edited / deleted
    comment = payload.get("comment", {})
    sender_login = comment.get("user", {}).get("login")
    body = comment.get("body", "")
    issue_number = payload.get("issue", {}).get("number")
    repo = payload.get("repository", {}).get("full_name")

    if not is_from_copilot(sender_login):
        log.info("issue_comment from non-Copilot user (%s) — ignoring", sender_login)
        return

    log.info(
        "COPILOT COMMENT [%s] repo=%s issue=#%s\n---\n%s\n---",
        action, repo, issue_number, body,
    )
    # TODO (step 2): send `body` to the classifier (DONE / BLOCKED / PROGRESS)


def handle_pull_request(payload: dict) -> None:
    action = payload.get("action")  # opened / synchronize / closed / etc
    pr = payload.get("pull_request", {})
    sender_login = pr.get("user", {}).get("login")
    repo = payload.get("repository", {}).get("full_name")

    if not is_from_copilot(sender_login):
        return

    log.info(
        "COPILOT PR EVENT [%s] repo=%s pr=#%s title=%r",
        action, repo, pr.get("number"), pr.get("title"),
    )


def handle_review_comment(payload: dict) -> None:
    comment = payload.get("comment", {})
    sender_login = comment.get("user", {}).get("login")
    if not is_from_copilot(sender_login):
        return

    repo = payload.get("repository", {}).get("full_name")
    pr_number = payload.get("pull_request", {}).get("number")
    log.info(
        "COPILOT REVIEW COMMENT repo=%s pr=#%s\n---\n%s\n---",
        repo, pr_number, comment.get("body", ""),
    )


@app.get("/health")
async def health():
    return {"status": "ok"}