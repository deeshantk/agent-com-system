"""
Driver loop — runs the LangGraph agent and handles the interrupt/resume
cycle end to end.

When the agent gets stuck, this now POSTs the question to the
orchestrator (which places the Twilio call), then polls for the
transcribed answer instead of prompting in the terminal.
"""

import os
import time
import uuid

import requests
from langgraph.types import Command

from agent import graph

ORCHESTRATOR_URL = os.environ["ORCHESTRATOR_URL"].rstrip("/")
POLL_INTERVAL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 5 * 60  # give up waiting for an answer after 5 minutes


def ask_via_twilio(question: str, session_id: str) -> str:
    print("\n🔴 Agent is blocked and needs input — calling you now...")
    print(f"   {question}\n")

    resp = requests.post(
        f"{ORCHESTRATOR_URL}/ask",
        json={"session_id": session_id, "question": question},
        timeout=30,
    )
    resp.raise_for_status()

    waited = 0
    while waited < POLL_TIMEOUT_SECONDS:
        r = requests.get(f"{ORCHESTRATOR_URL}/answer/{session_id}", timeout=30)
        if r.status_code == 200:
            answer = r.json()["answer"]
            print(f"✅ Got answer from call: {answer}\n")
            return answer
        # 202 = still pending, keep polling
        time.sleep(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS

    raise TimeoutError(f"No answer received for session {session_id} within {POLL_TIMEOUT_SECONDS}s")


def run_task(task_text: str) -> None:
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    print(f"\n=== Starting task (thread_id={thread_id}) ===\n{task_text}\n")

    result = graph.invoke({"messages": [("user", task_text)]}, config=config)

    # A graph can (in theory) hit multiple interrupts across a run if it
    # asks more than one question — loop until it stops interrupting.
    # Each question in the same run reuses thread_id as the session_id,
    # which is fine since the orchestrator only ever has one pending
    # question per session at a time.
    while "__interrupt__" in result:
        interrupt_obj = result["__interrupt__"][0]
        question = interrupt_obj.value["question"]

        answer = ask_via_twilio(question, session_id=thread_id)

        result = graph.invoke(Command(resume=answer), config=config)

    print("\n✅ Done. Final conversation:\n")
    for m in result["messages"]:
        m.pretty_print()


if __name__ == "__main__":
    task = """\
Send a test SMS via Twilio.

Requirements:
- Use Twilio's REST API to send an SMS
- The message body can be simple, e.g. "test notification"

Important: I haven't told you which phone number to send the SMS to, or
which Twilio credentials/env vars to use. Don't guess or invent
placeholder values for these.
"""
    run_task(task)