"""WhatsApp webhooks (Twilio). Verify, de-duplicate, queue, return 200 fast.

Twilio posts application/x-www-form-urlencoded and signs each request with
X-Twilio-Signature. Replies are sent from the worker, so the response is empty TwiML.
Unlike Meta's Cloud API there is no GET verification handshake.
"""

import logging
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.errors import AppError
from app.redis import get_redis
from app.services import idempotency
from app.services.whatsapp import Params, get_provider
from app.workers.tasks import handle_inbound, update_message_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])

MAX_BODY_BYTES = 64 * 1024
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _twiml() -> Response:
    return Response(content=EMPTY_TWIML, media_type="text/xml")


async def _verified_params(request: Request) -> Params:
    """Parse the form body and check the provider signature; 403 on any mismatch."""
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        raise AppError("payload_too_large", "Request body too large", status_code=413)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise AppError("payload_too_large", "Request body too large", status_code=413)
    try:
        params = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError as exc:
        raise AppError("bad_request", "Body must be UTF-8 form data") from exc

    # Signatures cover the public URL Twilio called, not the internal one.
    url = get_settings().public_base_url.rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    if not get_provider().verify_signature(url, params, request.headers.get("x-twilio-signature")):
        logger.warning("webhook signature rejected")
        raise AppError("invalid_signature", "Webhook signature is invalid", status_code=403)
    return params


def _accept_inbound(params: Params) -> None:
    try:
        msg = get_provider().parse_inbound(params)
    except (ValidationError, ValueError):
        # Signed by the provider but not a message we handle: acknowledge, don't retry.
        logger.warning("unparseable inbound webhook ignored")
        return
    client = get_redis()
    if not idempotency.claim(client, msg.message_id, get_settings().idempotency_ttl_s):
        logger.info("duplicate webhook delivery ignored")
        return
    try:
        handle_inbound.delay(msg.model_dump())
    except Exception:
        idempotency.release(client, msg.message_id)  # let the provider's retry through
        raise


@router.post("", response_class=Response)
async def receive_message(request: Request) -> Response:
    params = await _verified_params(request)
    try:
        await run_in_threadpool(_accept_inbound, params)
    except Exception as exc:
        logger.exception("could not queue inbound message")
        raise AppError("unavailable", "Could not queue message", status_code=503) from exc
    return _twiml()


@router.post("/status", response_class=Response)
async def receive_status(request: Request) -> Response:
    params = await _verified_params(request)
    try:
        status = get_provider().parse_status(params)
    except (ValidationError, ValueError):
        logger.warning("unparseable status webhook ignored")
        return _twiml()
    try:
        await run_in_threadpool(update_message_status.delay, status.model_dump())
    except Exception as exc:
        logger.exception("could not queue status update")
        raise AppError("unavailable", "Could not queue status update", status_code=503) from exc
    return _twiml()
