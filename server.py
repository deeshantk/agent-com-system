"""
Orchestrator — the one piece of this system that needs to be publicly
reachable. Everything else (the LangGraph agent + driver) can run on your
machine or an internal VM and only ever makes OUTBOUND calls to this
service.

Flow:
  1. Driver POSTs a question to /ask  (outbound from driver)
  2. This service stores the session in Redis
  3. This service calls Twilio's REST API to dial you
  4. Twilio hits /voice/twiml when the call connects
     -> we return TwiML that speaks the question and gathers your
        spoken answer
  5. Twilio hits /voice/answer with the transcribed speech
     -> we store the answer in Redis
  6. Driver polls GET /answer/{session_id}
     -> returns the answer once it is ready

Redis is used instead of an in-memory dict so that the session survives
process restarts and can be accessed regardless of which server process
handles the request.
"""

import logging
import os

import redis

from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Gather, VoiceResponse


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("orchestrator")


# ============================================================
# FastAPI
# ============================================================

app = FastAPI()


# ============================================================
# Environment variables
# ============================================================

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]

TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]
TWILIO_TO_NUMBER = os.environ["TWILIO_TO_NUMBER"]

# Public URL of this Render service.
#
# Example:
# https://my-orchestrator.onrender.com
#
# No trailing slash.
BASE_URL = os.environ["ORCHESTRATOR_BASE_URL"].rstrip("/")


# ============================================================
# Redis configuration
# ============================================================

REDIS_HOST = os.environ["REDIS_HOST"]
REDIS_PORT = int(os.environ["REDIS_PORT"])
REDIS_USERNAME = os.environ["REDIS_USERNAME"]
REDIS_PASSWORD = os.environ["REDIS_PASSWORD"]


# Redis Cloud client.
#
# decode_responses=True means Redis returns normal Python strings
# instead of bytes.
redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    username=REDIS_USERNAME,
    password=REDIS_PASSWORD,
    decode_responses=True,
)


# ============================================================
# Twilio client
# ============================================================

twilio_client = TwilioClient(
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
)


# ============================================================
# Constants
# ============================================================

# Sessions will automatically disappear after this amount of time.
#
# This prevents abandoned sessions from accumulating in Redis.
SESSION_TTL_SECONDS = 10 * 60


def session_key(session_id: str) -> str:
    """
    Convert our application session ID into a Redis key.

    Example:
        session_id:
            b893e3a9-50a3-4a1a-b46d-0652771684a8

        Redis key:
            session:b893e3a9-50a3-4a1a-b46d-0652771684a8
    """
    return f"session:{session_id}"


# ============================================================
# Startup
# ============================================================

@app.on_event("startup")
async def startup():
    """
    Verify that Redis is reachable when the application starts.
    """

    try:
        redis_client.ping()
        log.info("Redis connection successful")

    except Exception:
        log.exception("Redis connection failed")
        raise


# ============================================================
# /ask
# ============================================================

@app.post("/ask")
async def ask(request: Request):
    body = await request.json()
    session_id = body["session_id"]
    question = body["question"]
    agent_name = body.get("agent_name", "AI Agent")

    redis_key = f"session:{session_id}"

    redis_client.hset(
        redis_key,
        mapping={
            "agent_name": agent_name,
            "question": question,
            "answer": "",
        },
    )

    redis_client.expire(
        redis_key,
        10 * 60,
    )

    log.info("New session %s: %s", session_id, question)

    # Remember this as the "active" session so an INBOUND call (you
    # dialing the Twilio number yourself) knows which question to read
    # out, without needing a session_id in the URL.
    redis_client.set("latest_session_id", session_id, ex=SESSION_TTL_SECONDS)

    # KEEP THIS EXACTLY LIKE YOUR ORIGINAL VERSION
    call = twilio_client.calls.create(
        to=TWILIO_TO_NUMBER,
        from_=TWILIO_FROM_NUMBER,
        url=f"{BASE_URL}/voice/twiml?session_id={session_id}",
    )

    log.info(
        "Placed call %s for session %s",
        call.sid,
        session_id,
    )

    return {
        "ok": True,
        "call_sid": call.sid,
    }


# ============================================================
# /voice/twiml
# ============================================================

@app.api_route(
    "/voice/twiml",
    methods=["GET", "POST"],
)
async def voice_twiml(session_id: str):
    """
    Twilio hits this when the phone call connects.

    We:
        1. Read the question from Redis
        2. Tell Twilio to speak the question
        3. Tell Twilio to listen for the user's answer
        4. Tell Twilio where to send the transcription
    """

    redis_key = session_key(session_id)

    # --------------------------------------------------------
    # Retrieve session from Redis
    # --------------------------------------------------------

    session = redis_client.hgetall(redis_key)

    if session:
        agent_name = session.get("agent_name", "AI Agent")
        question = session.get("question", "")
    else:
        agent_name = "AI Agent"
        question = "No question found for this session."

        log.warning(
            "No Redis session found for %s when generating TwiML",
            session_id,
        )

    log.info(
        "Generating TwiML for session %s",
        session_id,
    )

    # --------------------------------------------------------
    # Build TwiML response
    # --------------------------------------------------------

    response = VoiceResponse()

    gather = Gather(
        input="speech",

        # After speech recognition, Twilio will POST
        # the transcription here.
        action=(
            f"{BASE_URL}/voice/answer"
            f"?session_id={session_id}"
        ),

        # Wait up to 20 seconds for the caller to begin speaking.
        timeout=20,
        # "auto" uses Twilio's speech-completion model to detect when
        # you're actually done talking, instead of guessing off a fixed
        # silence window — a fixed number (e.g. "10") can and did trigger
        # on a mid-sentence pause and cut the call off early.
        speech_timeout="auto",
        # Send empty results to /voice/answer so we can retry.
        action_on_empty_result=True,

        method="POST",
    )

    gather.say(
        f"Hi, this is {agent_name}. "
        "I need your help to continue. "
        f"{question} "
        "Take a moment to think, then speak your answer."
    )

    response.append(gather)

    # --------------------------------------------------------
    # If no speech was detected
    # --------------------------------------------------------

    response.say(
        "I didn't catch that. Goodbye."
    )

    response.hangup()

    # --------------------------------------------------------
    # Return TwiML
    # --------------------------------------------------------

    return PlainTextResponse(
        content=str(response),
        media_type="application/xml",
    )


# ============================================================
# /voice/inbound
# ============================================================

@app.api_route("/voice/inbound", methods=["GET", "POST"])
async def voice_inbound():
    """
    Hit when YOU call the Twilio number (not when Twilio calls you).

    Requires a one-time Twilio console change: on your phone number's
    config page, set "A call comes in" -> Webhook ->
    {BASE_URL}/voice/inbound, HTTP POST.

    Looks up whatever session is currently pending (no answer yet) and
    lets you answer it by voice, same as an outbound call would.
    """
    session_id = redis_client.get("latest_session_id")
    session = redis_client.hgetall(session_key(session_id)) if session_id else None

    response = VoiceResponse()

    if not session or session.get("answer"):
        # No session, or it already has an answer — nothing pending.
        response.say("There's no pending question right now. Goodbye.")
        response.hangup()
        return PlainTextResponse(content=str(response), media_type="application/xml")

    agent_name = session.get("agent_name", "AI Agent")
    question = session.get("question", "")

    gather = Gather(
        input="speech",
        action=f"{BASE_URL}/voice/answer?session_id={session_id}",
        timeout=20,
        speech_timeout="auto",
        action_on_empty_result=True,
        method="POST",
    )
    gather.say(f"This is {agent_name}. {question} Go ahead.")
    response.append(gather)

    response.say("I didn't catch that. Goodbye.")
    response.hangup()

    return PlainTextResponse(content=str(response), media_type="application/xml")


# ============================================================
# /voice/answer
# ============================================================

@app.post("/voice/answer")
async def voice_answer(
    session_id: str,
    SpeechResult: str = Form(default=""),
):
    """
    Twilio POSTs here after speech recognition.

    SpeechResult contains the transcription of what the user said.

    Example:

        SpeechResult = "Use the Twilio account SID
                        and auth token from environment variables"
    """

    redis_key = session_key(session_id)

    # --------------------------------------------------------
    # Check whether session exists
    # --------------------------------------------------------

    if redis_client.exists(redis_key):

        # ----------------------------------------------------
        # Store answer in Redis
        # ----------------------------------------------------

        redis_client.hset(
            redis_key,
            "answer",
            SpeechResult,
        )

        # Refresh the TTL.
        #
        # This isn't strictly necessary, but gives the local
        # driver some extra time to retrieve the answer.
        redis_client.expire(
            redis_key,
            SESSION_TTL_SECONDS,
        )

        log.info(
            "Session %s answered: %s",
            session_id,
            SpeechResult,
        )

    else:

        log.warning(
            "Got answer for unknown session %s",
            session_id,
        )

    # If the caller was silent, keep the call open and ask again.
    if not SpeechResult.strip():
        session = redis_client.hgetall(redis_key)
        response = VoiceResponse()
        if not session:
            response.say("I could not find this session. Goodbye.")
            response.hangup()
        else:
            agent_name = session.get("agent_name", "AI Agent")
            question = session.get("question", "")
            gather = Gather(
                input="speech",
                action=f"{BASE_URL}/voice/answer?session_id={session_id}",
                timeout=10,
                speech_timeout="auto",
                action_on_empty_result=True,
                method="POST",
            )
            gather.say(
                f"This is {agent_name}. "
                "I didn't hear an answer. Take your time. "
                f"{question}"
            )
            response.append(gather)
            response.say("I still didn't hear an answer. Goodbye.")
            response.hangup()

        return PlainTextResponse(
            content=str(response),
            media_type="application/xml",
        )

    # --------------------------------------------------------
    # Tell caller we're done
    # --------------------------------------------------------

    response = VoiceResponse()
    response.say("Got it, thanks. Goodbye.")
    response.hangup()

    return PlainTextResponse(
        content=str(response),
        media_type="application/xml",
    )


# ============================================================
# /answer/{session_id}
# ============================================================

@app.get("/answer/{session_id}")
async def get_answer(session_id: str):
    """
    Polled by the LangGraph driver.

    Returns:

        202 -> session exists but answer isn't ready

        200 -> answer is ready

        404 -> session doesn't exist
    """

    redis_key = session_key(session_id)

    # --------------------------------------------------------
    # Retrieve session
    # --------------------------------------------------------

    session = redis_client.hgetall(redis_key)

    # --------------------------------------------------------
    # Session doesn't exist
    # --------------------------------------------------------

    if not session:

        log.warning(
            "Session %s not found in Redis",
            session_id,
        )

        return PlainTextResponse(
            "unknown session_id",
            status_code=404,
        )

    # --------------------------------------------------------
    # Check answer
    # --------------------------------------------------------

    answer = session.get("answer", "")

    if not answer:

        return PlainTextResponse(
            "pending",
            status_code=202,
        )

    # --------------------------------------------------------
    # Answer ready
    # --------------------------------------------------------

    log.info(
        "Returning answer for session %s",
        session_id,
    )

    return {
        "answer": answer,
    }


# ============================================================
# /
# ============================================================

@app.get("/")
async def root():
    return {
        "service": "orchestrator",
        "status": "running",
    }


# ============================================================
# /health
# ============================================================

@app.get("/health")
async def health():
    """
    Health endpoint.

    Also verifies Redis connectivity.
    """

    try:
        redis_client.ping()
        redis_status = True

    except Exception:
        redis_status = False

    return {
        "status": "ok",
        "redis": redis_status,
    }