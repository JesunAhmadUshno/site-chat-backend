"""site-chat gateway: the public chat endpoint for the portfolio sites.

FastAPI app. It holds no project context of its own. On every /chat
request it fetches the site's grounded context from the MCP grounding
server (app/mcp_client.py, stdio transport), builds the prompt, and
calls the llama.cpp server on localhost, which serves
Qwen2.5-0.5B-Instruct (GGUF Q4_K_M), a small open-weight base model,
on the same fleet host. If the MCP server or the model is down, the
gateway answers with one plain busy message. It never falls back to a
local copy of the context and never guesses.

Contract (shared with the widget and the earlier Worker design):
  POST /chat { "site": slug, "message": text, "history": [turns] }
  message max 1000 chars, history max 6 turns of max 1000 chars each.
  CORS only for https://jesunahmadushno.github.io and
  https://jesunahmadushno.com. Per-IP soft limit: 20 requests per
  10 minutes, best-effort, in memory. Message contents are never
  logged; the log carries the site slug, the context size fetched
  through MCP, and an outcome word.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.mcp_client import McpClient, McpUnavailable, UnknownSite

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("site-chat")

LLAMA_URL = os.environ.get("LLAMA_URL", "http://127.0.0.1:8300")
LLAMA_MODEL = os.environ.get("LLAMA_MODEL", "qwen2.5-0.5b-instruct-q4_k_m")
MODEL_LABEL = (
    "Qwen2.5-0.5B-Instruct (GGUF Q4_K_M), a small open-weight base model, "
    "served by llama.cpp on our own fleet server. It is not fine-tuned yet; "
    "a fine-tune on the project corpus is planned v2."
)

ALLOWED_ORIGINS = {
    "https://jesunahmadushno.github.io",
    "https://jesunahmadushno.com",
}
MAX_MESSAGE_CHARS = 1000
MAX_HISTORY_TURNS = 6
MAX_HISTORY_CHARS = 1000
RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 600

BUSY_MESSAGE = "The assistant is busy right now. Please try again in a little while."
DECLINE_MESSAGE = "I do not know. That is not covered in this project's published results."
SUPPORT_MESSAGE = (
    "You can support the author here: Sponsor: "
    "https://github.com/sponsors/JesunAhmadUshno - Buy Me a Coffee: "
    "https://buymeacoffee.com/jesun - Work with me (book a 30 minute call): "
    "https://calendly.com/jesunahmadushno/30min"
)
SIMULATED_PREFIX = "This was a simulated study, run with a scripted approver, not a person. "

# The model is small, so the gateway also runs a conservative off-topic
# gate over the MCP-fetched context: if a question shares no content
# word with the site's context (and is not a support or hiring
# question, and not a meta question about the project itself), the
# gateway declines directly instead of letting the model improvise.
STOPWORDS = {
    "what", "which", "when", "where", "who", "whom", "whose", "why", "how",
    "is", "are", "was", "were", "do", "does", "did", "can", "could", "would",
    "should", "the", "a", "an", "of", "in", "on", "at", "to", "for", "with",
    "and", "or", "about", "tell", "me", "give", "show", "explain", "please",
    "it", "its", "this", "that", "there", "any", "many", "much", "often",
}
META_WORDS = {
    "project", "site", "page", "repo", "repository", "author", "jesun",
    "built", "build", "made", "created", "study", "results", "dashboard",
}
SUPPORT_WORDS = {
    "hire", "hiring", "sponsor", "sponsorship", "support", "donate",
    "donation", "coffee", "contact", "work", "job", "consult", "freelance",
}


def content_words(text):
    words = set()
    token = ""
    for ch in text.lower():
        if ch.isalnum():
            token += ch
        else:
            if token:
                words.add(token)
            token = ""
    if token:
        words.add(token)
    return words


def off_topic(message, context):
    words = content_words(message)
    if words & SUPPORT_WORDS:
        return False
    content = words - STOPWORDS
    if not content:
        return False
    if content & META_WORDS:
        return False
    return not (content & content_words(context))

SYSTEM_RULES = """You are the site assistant on a portfolio project page.
Answer questions about this one project, in simple words, using only the context below, which was fetched live from the project's grounding server.
If the answer is not in the context, say plainly that you do not know, and do not guess.
Never invent numbers, results, clients, employers, or experience. Use the figures exactly as written in the context.
When the visitor asks how much, how many, or how often, lead with the measured figure from the context.
Some contexts describe a SIMULATED study. If the context you received mentions SIMULATED, begin your answer with the words: This was a simulated study. Then give the figure.
Where it states a claims ceiling or a limit, respect it and mention it when it matters.
Keep answers short: two to five sentences unless the visitor asks for detail.
If the visitor asks how to support the author or hire him, you may share these links: Sponsor: https://github.com/sponsors/JesunAhmadUshno - Buy Me a Coffee: https://buymeacoffee.com/jesun - Work with me: https://calendly.com/jesunahmadushno/30min

CONTEXT
{context}"""

app = FastAPI(title="site-chat gateway", docs_url=None, redoc_url=None, openapi_url=None)
mcp = McpClient(server_path=os.environ.get("MCP_SERVER_PATH") or None)

_rate_buckets = defaultdict(deque)


def rate_limited(ip, now):
    bucket = _rate_buckets[ip]
    while bucket and now - bucket[0] >= RATE_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT:
        return True
    bucket.append(now)
    return False


def cors_headers(origin):
    if origin in ALLOWED_ORIGINS:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
            "Vary": "Origin",
        }
    return {}


def respond(status, body, origin):
    return JSONResponse(status_code=status, content=body, headers=cors_headers(origin))


@app.middleware("http")
async def preflight(request, call_next):
    if request.method == "OPTIONS":
        return JSONResponse(
            status_code=204,
            content=None,
            headers=cors_headers(request.headers.get("origin")),
        )
    return await call_next(request)


def validate(body):
    if not isinstance(body, dict):
        return "Request body must be a JSON object."
    site = body.get("site")
    message = body.get("message")
    history = body.get("history", [])
    if not isinstance(site, str) or not site:
        return "Unknown site."
    if not isinstance(message, str) or not message.strip():
        return "Message must be a non-empty string."
    if len(message) > MAX_MESSAGE_CHARS:
        return "Message is too long (max %d characters)." % MAX_MESSAGE_CHARS
    if not isinstance(history, list) or len(history) > MAX_HISTORY_TURNS:
        return "History must be an array of at most %d turns." % MAX_HISTORY_TURNS
    for turn in history:
        if (
            not isinstance(turn, dict)
            or turn.get("role") not in ("user", "assistant")
            or not isinstance(turn.get("content"), str)
            or len(turn["content"]) > MAX_HISTORY_CHARS
        ):
            return "Each history turn needs a role of user or assistant and content of at most %d characters." % MAX_HISTORY_CHARS
    return None


def call_model(messages):
    payload = json.dumps({
        "model": LLAMA_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 384,
    }).encode("utf-8")
    req = urllib.request.Request(
        LLAMA_URL + "/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


@app.get("/health")
def health(request: Request):
    origin = request.headers.get("origin")
    model_up = False
    try:
        req = urllib.request.Request(LLAMA_URL + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            model_up = resp.status == 200
    except Exception:
        model_up = False
    return respond(200, {
        "ok": True,
        "mcp_server": "up" if mcp.ping() else "down",
        "model_server": "up" if model_up else "down",
        "model": MODEL_LABEL,
    }, origin)


@app.get("/info")
def info(request: Request):
    return respond(200, {
        "service": "site-chat gateway",
        "model": MODEL_LABEL,
        "grounding": "Per-site context is fetched on every request from our own MCP grounding server (stdio transport). The gateway holds no context of its own.",
        "stack": "FastAPI gateway, MCP grounding server, llama.cpp model server, all on our own fleet host.",
    }, request.headers.get("origin"))


@app.post("/chat")
async def chat(request: Request):
    origin = request.headers.get("origin")
    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        return respond(415, {"error": "JSON only."}, origin)
    try:
        body = await request.json()
    except Exception:
        return respond(400, {"error": "Body must be valid JSON."}, origin)
    problem = validate(body)
    if problem:
        return respond(400, {"error": problem}, origin)

    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")
    if rate_limited(ip, time.time()):
        log.info("chat outcome=rate_limited site=%s", body["site"])
        return respond(429, {"error": BUSY_MESSAGE}, origin)

    try:
        context = mcp.get_site_context(body["site"])
    except UnknownSite:
        return respond(400, {"error": "Unknown site."}, origin)
    except McpUnavailable:
        log.info("chat outcome=mcp_down site=%s", body["site"])
        return respond(503, {"error": BUSY_MESSAGE}, origin)
    log.info("mcp_context site=%s chars=%d", body["site"], len(context))

    if off_topic(body["message"], context):
        log.info("chat outcome=off_topic_gate site=%s", body["site"])
        return respond(200, {"reply": DECLINE_MESSAGE}, origin)

    if content_words(body["message"]) & SUPPORT_WORDS:
        log.info("chat outcome=support_links site=%s", body["site"])
        return respond(200, {"reply": SUPPORT_MESSAGE}, origin)

    messages = [{"role": "system", "content": SYSTEM_RULES.format(context=context)}]
    for turn in body.get("history", []):
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": body["message"]})

    try:
        reply = call_model(messages)
    except Exception:
        log.info("chat outcome=model_down site=%s", body["site"])
        return respond(503, {"error": BUSY_MESSAGE}, origin)
    if not reply:
        log.info("chat outcome=empty_reply site=%s", body["site"])
        return respond(503, {"error": BUSY_MESSAGE}, origin)
    # Label guard: if the context marks its results SIMULATED, the
    # label must survive into the answer even when the small model
    # drops it. This is a presentation rule, applied openly.
    if "SIMULATED" in context and "simulat" not in reply.lower():
        reply = SIMULATED_PREFIX + reply
    log.info("chat outcome=ok site=%s", body["site"])
    return respond(200, {"reply": reply}, origin)
