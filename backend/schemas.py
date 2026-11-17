import math
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID


ID_PATTERN = re.compile(r"^[A-Z0-9_]{1,64}$")
MESSAGE_TYPES = {"telemetry", "status", "event", "command_ack"}
SEVERITIES = {"INFO", "WARNING", "HIGH", "CRITICAL"}
QUALITIES = {"GOOD", "BAD", "UNCERTAIN"}
AI_COUNT_PARAMETERS = {
    "cow_count",
    "farmer_count",
    "intruder_count",
    "animal_anomaly_count",
}
STATE_PARAMETERS = {"gate_state", "alarm_state", "feed_status"}
GROUP3_PERCENT_PARAMETERS = {"feed_level_percent", "water_level_percent"}
PULSE_RAW_PARAMETER = "pulse_raw"
HEART_RATE_PARAMETER = "heart_rate_bpm"


class ValidationError(ValueError):
    pass


def parse_timestamp(value: Any, field: str = "timestamp") -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be an ISO 8601 timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO 8601 timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValidationError(f"{field} must include a timezone")
    return result.astimezone(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValidationError(f"{field} must contain uppercase letters, digits, or underscores")
    return value


def require_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a UUID")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValidationError(f"{field} must be a UUID") from exc


def require_text(value: Any, field: str, max_length: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValidationError(f"{field} must be a non-empty string of at most {max_length} characters")
    return value.strip()


def validate_stream_url(value: Any) -> str | None:
    if value is None or value == "":
        return None
    url = require_text(value, "stream_url", 2048)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValidationError("stream_url must use http or https")
    return url


def validate_envelope(data: Any, expected_type: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValidationError("message must be a JSON object")
    data = deepcopy(data)
    if data.get("schema_version") != "1.0.0":
        raise ValidationError("schema_version must be 1.0.0")
    if data.get("message_type") != expected_type:
        raise ValidationError(f"message_type must be {expected_type}")
    if expected_type not in MESSAGE_TYPES:
        raise ValidationError("unsupported message_type")

    data["message_id"] = require_uuid(data.get("message_id"), "message_id")
    data["timestamp"] = parse_timestamp(data.get("timestamp"))
    for field in ("farm_id", "barn_id", "device_id"):
        data[field] = require_id(data.get(field), field)
    if data.get("zone_id") is not None:
        data["zone_id"] = require_id(data["zone_id"], "zone_id")
    if not isinstance(data.get("payload"), dict):
        raise ValidationError("payload must be a JSON object")

    payload = data["payload"]
    if expected_type == "telemetry":
        readings = payload.get("readings")
        if not isinstance(readings, list) or not readings:
            raise ValidationError("payload.readings must be a non-empty array")
        seen_readings = set()
        for index, reading in enumerate(readings):
            prefix = f"payload.readings[{index}]"
            if not isinstance(reading, dict):
                raise ValidationError(f"{prefix} must be an object")
            reading["sensor_id"] = require_id(reading.get("sensor_id"), f"{prefix}.sensor_id")
            reading["parameter"] = require_text(reading.get("parameter"), f"{prefix}.parameter", 64)
            reading_key = (reading["sensor_id"], reading["parameter"])
            if reading_key in seen_readings:
                raise ValidationError(f"{prefix} duplicates a sensor_id/parameter reading")
            seen_readings.add(reading_key)
            reading["unit"] = require_text(reading.get("unit"), f"{prefix}.unit", 32)
            value = reading.get("value")
            if reading["parameter"] in AI_COUNT_PARAMETERS:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValidationError(f"{prefix}.value must be a non-negative integer")
            elif reading["parameter"] == "distance_cm":
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not 2 <= value <= 400
                ):
                    raise ValidationError(f"{prefix}.value must be between 2 and 400 cm")
            elif reading["parameter"] in GROUP3_PERCENT_PARAMETERS:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not 0 <= value <= 100
                ):
                    raise ValidationError(f"{prefix}.value must be between 0 and 100 percent")
            elif reading["parameter"] == PULSE_RAW_PARAMETER:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 0 <= value <= 4095
                ):
                    raise ValidationError(f"{prefix}.value must be an ADC integer from 0 to 4095")
            elif reading["parameter"] == HEART_RATE_PARAMETER:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0 <= value <= 300
                    or not math.isfinite(value)
                ):
                    raise ValidationError(f"{prefix}.value must be between 0 and 300 bpm")
            elif reading["parameter"] in STATE_PARAMETERS:
                if not isinstance(value, str) or not value.strip() or len(value) > 32:
                    raise ValidationError(f"{prefix}.value must be a non-empty state string")
                reading["value"] = value.strip()
                if (
                    reading["parameter"] == "feed_status"
                    and reading["value"] not in {"LOW", "NORMAL", "ERROR"}
                ):
                    raise ValidationError("feed_status must be LOW, NORMAL, or ERROR")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValidationError(f"{prefix}.value must be a finite number")
            reading["quality"] = reading.get("quality", "GOOD")
            if not isinstance(reading["quality"], str) or reading["quality"] not in QUALITIES:
                raise ValidationError(f"{prefix}.quality must be GOOD, BAD, or UNCERTAIN")
    elif expected_type == "status":
        if payload.get("connection_state") not in ("ONLINE", "OFFLINE"):
            raise ValidationError("payload.connection_state must be ONLINE or OFFLINE")
        if payload.get("operational_state") is not None and not isinstance(
            payload["operational_state"], str
        ):
            raise ValidationError("payload.operational_state must be a string")
        if payload.get("firmware_version") is not None and not isinstance(
            payload["firmware_version"], str
        ):
            raise ValidationError("payload.firmware_version must be a string")
        uptime = payload.get("uptime_seconds")
        if uptime is not None and (
            isinstance(uptime, bool) or not isinstance(uptime, int) or uptime < 0
        ):
            raise ValidationError("payload.uptime_seconds must be a non-negative integer")
    elif expected_type == "event":
        payload["event_id"] = require_uuid(payload.get("event_id"), "payload.event_id")
        payload["event_type"] = require_text(payload.get("event_type"), "payload.event_type", 64)
        if not isinstance(payload.get("severity"), str) or payload["severity"] not in SEVERITIES:
            raise ValidationError("payload.severity must be INFO, WARNING, HIGH, or CRITICAL")
        if payload.get("state", "ACTIVE") not in ("ACTIVE", "CLEARED"):
            raise ValidationError("payload.state must be ACTIVE or CLEARED")
        payload["state"] = payload.get("state", "ACTIVE")
        if payload.get("message") is not None:
            payload["message"] = require_text(payload["message"], "payload.message", 500)
        if payload.get("parameter") is not None:
            payload["parameter"] = require_text(payload["parameter"], "payload.parameter", 64)
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValidationError("payload.metadata must be an object")
        if payload.get("uid") is not None and "uid" not in metadata:
            metadata["uid"] = payload["uid"]
        if payload["event_type"] in {"RFID_ACCESS_GRANTED", "RFID_ACCESS_DENIED"}:
            metadata["uid"] = require_text(metadata.get("uid"), "payload.metadata.uid", 64)
        payload["metadata"] = metadata
        for field in ("value", "threshold"):
            value = payload.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValidationError(f"payload.{field} must be a finite number")
    elif expected_type == "command_ack":
        payload["command_id"] = require_uuid(payload.get("command_id"), "payload.command_id")
        if payload.get("actuator_id") is not None:
            payload["actuator_id"] = require_id(payload["actuator_id"], "payload.actuator_id")
        if data["device_id"] == "DEV_004" and payload.get("actuator_id") is None:
            raise ValidationError("payload.actuator_id is required for DEV_004 command acknowledgements")
        if not isinstance(payload.get("result"), str) or payload["result"] not in {
            "ACCEPTED", "REJECTED", "FAILED"
        }:
            raise ValidationError("payload.result must be ACCEPTED, REJECTED, or FAILED")
        if payload.get("message") is not None:
            payload["message"] = require_text(payload["message"], "payload.message", 500)

    return data
