from __future__ import annotations

import json
import logging
import time
from typing import Any

LOGGER = logging.getLogger("pulse.bedrock")


def _client(config: dict):
    import boto3
    from botocore.config import Config

    kwargs = {
        "region_name": config.get("AWS_REGION", "us-east-1"),
        "config": Config(connect_timeout=3, read_timeout=20, retries={"max_attempts": 0}),
    }
    if config.get("AWS_ACCESS_KEY_ID") and config.get("AWS_SECRET_ACCESS_KEY"):
        kwargs.update({"aws_access_key_id": config["AWS_ACCESS_KEY_ID"], "aws_secret_access_key": config["AWS_SECRET_ACCESS_KEY"]})
    return boto3.client("bedrock-runtime", **kwargs)


def _json_output(response: dict) -> dict:
    text = "".join(item.get("text", "") for item in response.get("output", {}).get("message", {}).get("content", []) if isinstance(item, dict))
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Bedrock output was not an object")
    return value


def invoke_json(config: dict, instruction: str, payload: dict, schema: dict, fallback_model: bool = False) -> tuple[dict, dict]:
    model_id = config.get("BEDROCK_FALLBACK_MODEL_ID") if fallback_model else config.get("BEDROCK_MODEL_ID")
    if not model_id:
        raise RuntimeError("Bedrock model is not configured")
    prompt = json.dumps({"task": instruction, "input": payload, "output_schema": schema}, ensure_ascii=False, separators=(",", ":"))
    started = time.monotonic()
    response = _client(config).converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 700, "temperature": 0},
    )
    elapsed_ms = round((time.monotonic() - started) * 1000)
    usage = response.get("usage") or {}
    metadata = {"model": model_id, "latency_ms": elapsed_ms, "input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens"), "stop_reason": response.get("stopReason")}
    value = _json_output(response)
    if not all(key in value for key in schema):
        raise ValueError("Bedrock output failed required-field validation")
    return value, metadata


def classify_or_fallback(config: dict, instruction: str, payload: dict, schema: dict) -> tuple[dict | None, dict]:
    started = time.monotonic()
    try:
        value, metadata = invoke_json(config, instruction, payload, schema)
        metadata["success"] = True
        return value, metadata
    except Exception as exc:
        LOGGER.warning("bedrock primary request failed model=%s error=%s", config.get("BEDROCK_MODEL_ID"), type(exc).__name__)
        fallback = config.get("BEDROCK_FALLBACK_MODEL_ID")
        if fallback:
            try:
                value, metadata = invoke_json(config, instruction, payload, schema, fallback_model=True)
                metadata.update({"success": True, "escalated": True, "escalation_reason": type(exc).__name__})
                return value, metadata
            except Exception as fallback_exc:
                LOGGER.warning("bedrock fallback failed model=%s error=%s", fallback, type(fallback_exc).__name__)
        return None, {"success": False, "error": type(exc).__name__, "latency_ms": round((time.monotonic() - started) * 1000)}
