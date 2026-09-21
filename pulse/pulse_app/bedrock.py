from __future__ import annotations

import json
import hashlib
import logging
import time
from typing import Any

LOGGER = logging.getLogger("pulse.bedrock")
_CACHE: dict[str, tuple[float, dict, dict]] = {}


def credentials_present(config: dict) -> bool:
    return bool(config.get("AWS_ACCESS_KEY_ID") and config.get("AWS_SECRET_ACCESS_KEY"))


def validate_config(config: dict) -> dict:
    errors = []
    region = str(config.get("AWS_REGION", "")).strip()
    model = str(config.get("BEDROCK_MODEL_ID", "")).strip()
    if not region:
        errors.append("region_missing")
    if not model:
        errors.append("model_missing")
    if not credentials_present(config):
        errors.append("credentials_missing")
    return {"valid": not errors, "region": region, "model": model, "credentials_present": credentials_present(config), "errors": errors}


def safe_error_details(exc: Exception) -> dict:
    response = getattr(exc, "response", {}) or {}
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    return {"error_code": str(error.get("Code", type(exc).__name__))[:80], "error_message": str(error.get("Message", type(exc).__name__))[:500], "exception": type(exc).__name__}


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


def _validate_schema(value: dict, schema: dict) -> None:
    if not isinstance(value, dict):
        raise ValueError("Bedrock output was not an object")
    for key, expected in schema.items():
        if key not in value:
            raise ValueError("Bedrock output failed required-field validation")
        if expected == "string" and not isinstance(value[key], str):
            raise ValueError("Bedrock output failed string validation")
        if expected == "number" and not isinstance(value[key], (int, float)):
            raise ValueError("Bedrock output failed number validation")
        if expected == "boolean" and not isinstance(value[key], bool):
            raise ValueError("Bedrock output failed boolean validation")
        if expected == "array" and not isinstance(value[key], list):
            raise ValueError("Bedrock output failed array validation")
        if expected == "object" and not isinstance(value[key], dict):
            raise ValueError("Bedrock output failed object validation")


def invoke_json(config: dict, instruction: str, payload: dict, schema: dict, fallback_model: bool = False) -> tuple[dict, dict]:
    model_id = config.get("BEDROCK_FALLBACK_MODEL_ID") if fallback_model else config.get("BEDROCK_MODEL_ID")
    validation = validate_config(config)
    if not validation["valid"]:
        raise RuntimeError("Bedrock configuration is incomplete: " + ",".join(validation["errors"]))
    if not model_id:
        raise RuntimeError("Bedrock model is not configured")
    prompt = json.dumps({"task": instruction, "input": payload, "output_schema": schema}, ensure_ascii=False, separators=(",", ":"))
    cache_ttl = max(0, min(3600, int(config.get("BEDROCK_CACHE_TTL_SECONDS", 300))))
    cache_key = hashlib.sha256((model_id + ":" + prompt).encode()).hexdigest()
    cached = _CACHE.get(cache_key)
    if cached and cache_ttl and time.monotonic() - cached[0] < cache_ttl:
        value, metadata = cached[1], {**cached[2], "cached": True, "latency_ms": 0}
        return value, metadata
    started = time.monotonic()
    transient = {"TimeoutError", "ReadTimeoutError", "EndpointConnectionError", "ConnectionClosedError"}
    for attempt in range(2):
        try:
            response = _client(config).converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 700, "temperature": 0},
            )
            break
        except Exception as exc:
            if attempt == 0 and type(exc).__name__ in transient:
                time.sleep(0.15)
                continue
            raise
    elapsed_ms = round((time.monotonic() - started) * 1000)
    usage = response.get("usage") or {}
    metadata = {"model": model_id, "latency_ms": elapsed_ms, "input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens"), "stop_reason": response.get("stopReason")}
    value = _json_output(response)
    _validate_schema(value, schema)
    metadata["cached"] = False
    if cache_ttl:
        _CACHE[cache_key] = (time.monotonic(), value, metadata)
    return value, metadata


def classify_or_fallback(config: dict, instruction: str, payload: dict, schema: dict) -> tuple[dict | None, dict]:
    started = time.monotonic()
    try:
        value, metadata = invoke_json(config, instruction, payload, schema)
        metadata["success"] = True
        return value, metadata
    except Exception as exc:
        error = safe_error_details(exc)
        LOGGER.warning("bedrock primary request failed model=%s error=%s", config.get("BEDROCK_MODEL_ID"), error["error_code"])
        fallback = config.get("BEDROCK_FALLBACK_MODEL_ID")
        if fallback:
            try:
                value, metadata = invoke_json(config, instruction, payload, schema, fallback_model=True)
                metadata.update({"success": True, "escalated": True, "escalation_reason": type(exc).__name__})
                return value, metadata
            except Exception as fallback_exc:
                fallback_error = safe_error_details(fallback_exc)
                LOGGER.warning("bedrock fallback failed model=%s error=%s", fallback, fallback_error["error_code"])
        return None, {"success": False, "error": error["exception"], "error_code": error["error_code"], "error_message": error["error_message"], "fallback": "deterministic", "safe_to_continue": True, "latency_ms": round((time.monotonic() - started) * 1000)}
