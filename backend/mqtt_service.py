import json
import logging
import re
from datetime import timedelta
from typing import Any

import paho.mqtt.client as mqtt
from sqlalchemy.exc import IntegrityError

from models import (
    Device,
    DeviceCommand,
    FarmEvent,
    MessageReceipt,
    SensorRegistry,
    Telemetry,
    as_utc,
    db,
    utc_now,
)
from schemas import ValidationError, validate_envelope


logger = logging.getLogger(__name__)
TOPIC_ID = re.compile(r"^[A-Z0-9_]{1,64}$")
TOPIC_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DEFAULT_TOPIC_PREFIX = "sf-kandang-sapih"
INBOUND_SUFFIXES = {"telemetry", "status", "event", "command/ack"}


def topic_for(
    device: Device,
    suffix: str,
    topic_prefix: str = DEFAULT_TOPIC_PREFIX,
) -> str:
    if suffix not in {"command", "config"}:
        raise ValueError(f"Unsupported outbound MQTT topic suffix: {suffix}")
    if not TOPIC_PREFIX_PATTERN.fullmatch(topic_prefix):
        raise ValueError("MQTT_TOPIC_PREFIX must be a lowercase topic segment")
    parts = ["farm", device.farm_id, "barn", device.barn_id]
    if device.zone_id:
        parts.extend(("zone", device.zone_id))
    parts.extend(("device", device.device_id, suffix))
    return "/".join((topic_prefix, *parts))


def parse_topic(
    topic: str,
    topic_prefix: str = DEFAULT_TOPIC_PREFIX,
) -> tuple[dict[str, str | None], str]:
    if not TOPIC_PREFIX_PATTERN.fullmatch(topic_prefix):
        raise ValueError("MQTT_TOPIC_PREFIX must be a lowercase topic segment")
    parts = topic.split("/")
    if not parts or parts[0] != topic_prefix:
        raise ValidationError("MQTT topic does not use the configured project prefix")
    parts = parts[1:]
    if len(parts) >= 9 and parts[0] == "farm" and parts[2] == "barn" and parts[4] == "zone":
        farm_id, barn_id, zone_id, device_id = parts[1], parts[3], parts[5], parts[7]
        suffix_parts = parts[8:]
    elif len(parts) >= 7 and parts[0] == "farm" and parts[2] == "barn" and parts[4] == "device":
        farm_id, barn_id, zone_id, device_id = parts[1], parts[3], None, parts[5]
        suffix_parts = parts[6:]
    else:
        raise ValidationError("MQTT topic does not match the registered farm/device format")

    for field, value in (("farm_id", farm_id), ("barn_id", barn_id), ("device_id", device_id)):
        if not TOPIC_ID.fullmatch(value):
            raise ValidationError(f"MQTT topic has an invalid {field}")
    if zone_id is not None and not TOPIC_ID.fullmatch(zone_id):
        raise ValidationError("MQTT topic has an invalid zone_id")
    suffix = "/".join(suffix_parts)
    if suffix not in INBOUND_SUFFIXES:
        raise ValidationError("MQTT topic suffix is not supported")
    return {
        "farm_id": farm_id,
        "barn_id": barn_id,
        "zone_id": zone_id,
        "device_id": device_id,
    }, suffix


class MQTTService:
    def __init__(self, app):
        self.app = app
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=app.config["MQTT_CLIENT_ID"],
        )
        username = app.config.get("MQTT_USERNAME")
        if username:
            self.client.username_pw_set(username, app.config.get("MQTT_PASSWORD"))
        if app.config["MQTT_TLS"]:
            self.client.tls_set()
        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.connected = False

    def start(self) -> None:
        host = self.app.config.get("MQTT_HOST")
        if not host:
            raise RuntimeError("MQTT_HOST is required when MQTT_ENABLED=true")
        self.client.connect_async(host, self.app.config["MQTT_PORT"], keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()
        self.connected = False

    def publish(self, topic: str, payload: dict[str, Any], qos: int = 1) -> None:
        if not self.connected:
            raise RuntimeError("MQTT broker is not connected")
        result = self.client.publish(
            topic, json.dumps(payload, separators=(",", ":"), ensure_ascii=False), qos=qos, retain=False
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed with result code {result.rc}")
        result.wait_for_publish(timeout=self.app.config["MQTT_PUBLISH_TIMEOUT"])
        if not result.is_published():
            raise TimeoutError("MQTT publish was not acknowledged before timeout")

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code.is_failure:
            logger.error("MQTT connection failed: %s", reason_code)
            self.connected = False
            return
        self.connected = True
        for suffix in sorted(INBOUND_SUFFIXES):
            prefix = self.app.config["MQTT_TOPIC_PREFIX"]
            client.subscribe(f"{prefix}/farm/+/barn/+/zone/+/device/+/{suffix}", qos=1)
            client.subscribe(f"{prefix}/farm/+/barn/+/device/+/{suffix}", qos=1)
        logger.info("Connected to MQTT broker and subscribed to inbound device topics")

    def _on_connect_fail(self, _client, _userdata) -> None:
        self.connected = False
        logger.warning("MQTT connection attempt failed; the client will retry")

    def _on_disconnect(
        self, _client, _userdata, _disconnect_flags, reason_code, _properties
    ) -> None:
        self.connected = False
        if reason_code.is_failure:
            logger.warning("Disconnected from MQTT broker: %s", reason_code)

    def _on_message(self, _client, _userdata, message) -> None:
        with self.app.app_context():
            try:
                payload = json.loads(message.payload.decode("utf-8"))
                process_incoming_message(
                    message.topic,
                    payload,
                    self.app.config["MQTT_TOPIC_PREFIX"],
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
                db.session.rollback()
                logger.exception("Rejected MQTT message topic=%s", message.topic)
            except Exception:
                db.session.rollback()
                logger.exception("Failed to process MQTT message topic=%s", message.topic)


def process_incoming_message(
    topic: str,
    payload: Any,
    topic_prefix: str = DEFAULT_TOPIC_PREFIX,
) -> bool:
    topic_ids, suffix = parse_topic(topic, topic_prefix)
    message_type = "command_ack" if suffix == "command/ack" else suffix
    envelope = validate_envelope(payload, message_type)
    for field, topic_value in topic_ids.items():
        if envelope.get(field) != topic_value:
            raise ValidationError(f"{field} in the envelope does not match the MQTT topic")

    device = db.session.get(Device, envelope["device_id"])
    if device is None:
        raise ValidationError("device_id is not registered")
    for field in ("farm_id", "barn_id", "zone_id"):
        if getattr(device, field) != envelope.get(field):
            raise ValidationError(f"{field} does not match the device registry")

    receipt = MessageReceipt(
        message_id=envelope["message_id"],
        device_id=device.device_id,
        message_type=message_type,
    )
    db.session.add(receipt)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        logger.info("Ignored duplicate MQTT message_id=%s device_id=%s", envelope["message_id"], device.device_id)
        return False

    message_time = envelope["timestamp"]
    body = envelope["payload"]
    device.last_seen_at = message_time
    if message_type == "telemetry":
        for reading in body["readings"]:
            is_group1_pulse_reading = device.device_id == "DEV_001" and (
                reading["sensor_id"] == "SNS_PULSE_001"
                or reading["parameter"] in {"pulse_raw", "heart_rate_bpm"}
            )
            if device.device_id in {"DEV_003", "DEV_004"} or is_group1_pulse_reading:
                sensor = db.session.get(SensorRegistry, reading["sensor_id"])
                if (
                    sensor is None
                    or sensor.device_id != device.device_id
                    or reading["parameter"] not in sensor.parameters
                ):
                    raise ValidationError(
                        f"{reading['sensor_id']} is not registered for "
                        f"{reading['parameter']} on {device.device_id}"
                    )
            db.session.add(
                Telemetry(
                    message_id=envelope["message_id"],
                    device_id=device.device_id,
                    sensor_id=reading["sensor_id"],
                    parameter=reading["parameter"],
                    value=reading["value"],
                    unit=reading["unit"],
                    quality=reading["quality"],
                    timestamp=message_time,
                )
            )
        device.connection_status = "ONLINE"
    elif message_type == "status":
        device.connection_status = body["connection_state"]
        device.operational_state = body.get("operational_state")
        device.firmware_version = body.get("firmware_version", device.firmware_version)
        device.uptime_seconds = body.get("uptime_seconds", device.uptime_seconds)
    elif message_type == "event":
        db.session.add(
            FarmEvent(
                event_id=body["event_id"],
                message_id=envelope["message_id"],
                device_id=device.device_id,
                event_type=body["event_type"],
                severity=body["severity"],
                state=body["state"],
                uid=body.get("metadata", {}).get("uid"),
                event_metadata=body.get("metadata", {}),
                parameter=body.get("parameter"),
                value=body.get("value"),
                threshold=body.get("threshold"),
                message=body.get("message"),
                payload=body,
                timestamp=message_time,
            )
        )
    else:
        command = db.session.get(DeviceCommand, body["command_id"])
        if command is None or command.device_id != device.device_id:
            raise ValidationError("command_id does not identify a command for this device")
        if body.get("actuator_id") not in (None, command.actuator_id):
            raise ValidationError("actuator_id does not match the command")
        command.ack_at = message_time
        command.ack_result = body["result"]
        command.actual_state = body.get("actual_state")
        command.ack_message = body.get("message")
        command.status = (
            "EXPIRED"
            if message_time > as_utc(command.expires_at)
            else body["result"]
        )

    db.session.commit()
    return True


def create_command_message(device: Device, command: DeviceCommand) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "message_id": command.message_id,
        "message_type": "command",
        "timestamp": command.created_at.isoformat().replace("+00:00", "Z"),
        "farm_id": device.farm_id,
        "barn_id": device.barn_id,
        "zone_id": device.zone_id,
        "device_id": device.device_id,
        "payload": {
            "command_id": command.command_id,
            "actuator_id": command.actuator_id,
            "command": command.command,
            "parameters": command.parameters,
            "issued_by": command.issued_by,
            "expires_at": command.expires_at.isoformat().replace("+00:00", "Z"),
        },
    }


def next_expiry(app) -> Any:
    return utc_now() + timedelta(seconds=app.config["COMMAND_TTL_SECONDS"])
