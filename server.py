"""
Orchestrator — the one piece of this system that needs to be publicly
reachable. Everything else (the LangGraph agent + driver) can run on your
machine or an internal VM and only ever makes OUTBOUND calls to this
service.

Flow:
  1. Driver POSTs a question to /ask  (outbound from driver — always works)
  2. This service calls Twilio's REST API to dial you
  3. Twilio hits /voice/twiml when the call connects -> we return TwiML
     that speaks the question and gathers your spoken answer
  4. Twilio hits /voice/answer with the transcribed speech -> we store it
  5. Driver polls GET /answer/{session_id} (outbound — always works) until
     the answer is ready

Storage is a plain in-memory dict — fine for local testing. If this
process restarts mid-call, pending sessions are lost; swap for Redis/a
DB before this needs to survive that.
"""

import logging
import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Gather, VoiceResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orchestrator")

app = FastAPI()

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]  # your Twilio number
TWILIO_TO_NUMBER = os.environ["TWILIO_TO_NUMBER"]  # YOUR phone, gets called
# Public base URL of THIS service once deployed, e.g. https://your-app.onrender.com
# (no trailing slash). Twilio needs this to know where to fetch TwiML from
# and where to POST the transcribed answer.
BASE_URL = os.environ["ORCHESTRATOR_BASE_URL"].rstrip("/")

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# session_id -> {"question": str, "answer": str | None}
SESSIONS: dict = {}


@app.post("/ask")
async def ask(request: Request):
    """
    Called by the LangGraph driver when its agent hits interrupt().
    Body: {"session_id": "...", "question": "..."}
    Places the outbound call and returns immediately — the driver polls
    /answer/{session_id} separately.
    """
    body = await request.json()
    session_id = body["session_id"]
    question = body["question"]

    SESSIONS[session_id] = {"question": question, "answer": None}
    log.info("New session %s: %s", session_id, question)

    call = twilio_client.calls.create(
        to=TWILIO_TO_NUMBER,
        from_=TWILIO_FROM_NUMBER,
        url=f"{BASE_URL}/voice/twiml?session_id={session_id}",
    )
    log.info("Placed call %s for session %s", call.sid, session_id)

    return {"ok": True, "call_sid": call.sid}


@app.get("/voice/twiml")
async def voice_twiml(session_id: str):
    """
    Twilio hits this the moment the call connects. Returns TwiML that
    speaks the question and gathers a spoken answer.
    """
    session = SESSIONS.get(session_id)
    question = session["question"] if session else "No question found for this session."

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"{BASE_URL}/voice/answer?session_id={session_id}",
        speech_timeout="auto",
        method="POST",
    )
    gather.say(f"Hi, this is your agent. It's stuck and needs input. {question}")
    response.append(gather)

    # If Gather times out with no speech, fall through to this instead of
    # silently hanging up.
    response.say("I didn't catch that. Goodbye.")

    return PlainTextResponse(content=str(response), media_type="application/xml")


@app.post("/voice/answer")
async def voice_answer(session_id: str, SpeechResult: str = Form(default="")):
    """
    Twilio POSTs here with the transcribed speech once you've answered.
    """
    if session_id in SESSIONS:
        SESSIONS[session_id]["answer"] = SpeechResult
        log.info("Session %s answered: %s", session_id, SpeechResult)
    else:
        log.warning("Got answer for unknown session %s", session_id)

    response = VoiceResponse()
    response.say("Got it, thanks. Goodbye.")
    response.hangup()
    return PlainTextResponse(content=str(response), media_type="application/xml")


@app.get("/answer/{session_id}")
async def get_answer(session_id: str):
    """
    Polled by the LangGraph driver. Returns 202 while waiting, 200 with
    the answer once Twilio has relayed it.
    """
    session = SESSIONS.get(session_id)
    if session is None:
        return PlainTextResponse("unknown session_id", status_code=404)
    if session["answer"] is None:
        return PlainTextResponse("pending", status_code=202)
    return {"answer": session["answer"]}


@app.get("/")
async def root():
    return {"service": "orchestrator", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok"}