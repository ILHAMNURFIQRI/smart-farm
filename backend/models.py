from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index


db = SQLAlchemy()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Device(db.Model):
    device_id = db.Column(db.String(64), primary_key=True)
    farm_id = db.Column(db.String(64), nullable=False, index=True)
    barn_id = db.Column(db.String(64), nullable=False, index=True)
    zone_id = db.Column(db.String(64))
    device_type = db.Column(db.String(64))
    device_name = db.Column(db.String(120))
    stream_url = db.Column(db.String(2048))
    connection_status = db.Column(db.String(16), nullable=False, default="UNKNOWN")
    operational_state = db.Column(db.String(32))
    firmware_version = db.Column(db.String(64))
    uptime_seconds = db.Column(db.Integer)
    config = db.Column(db.JSON, nullable=False, default=dict)
    last_seen_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class MessageReceipt(db.Model):
    message_id = db.Column(db.String(36), primary_key=True)
    device_id = db.Column(
        db.String(64), db.ForeignKey("device.device_id"), nullable=False, index=True
    )
    message_type = db.Column(db.String(24), nullable=False)
    received_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class Telemetry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.String(36), nullable=False)
    device_id = db.Column(
        db.String(64), db.ForeignKey("device.device_id"), nullable=False
    )
    sensor_id = db.Column(db.String(64), nullable=False)
    parameter = db.Column(db.String(64), nullable=False)
    value = db.Column(db.JSON, nullable=False)
    unit = db.Column(db.String(32), nullable=False)
    quality = db.Column(db.String(16), nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_telemetry_device_timestamp", "device_id", "timestamp"),
        Index("ix_telemetry_sensor_timestamp", "sensor_id", "timestamp"),
        db.UniqueConstraint(
            "message_id", "sensor_id", "parameter",
            name="uq_telemetry_message_sensor_parameter",
        ),
    )


class FarmEvent(db.Model):
    event_id = db.Column(db.String(36), primary_key=True)
    message_id = db.Column(db.String(36), nullable=False, unique=True)
    device_id = db.Column(
        db.String(64), db.ForeignKey("device.device_id"), nullable=False, index=True
    )
    event_type = db.Column(db.String(64), nullable=False)
    severity = db.Column(db.String(16), nullable=False, index=True)
    state = db.Column(db.String(16), nullable=False)
    parameter = db.Column(db.String(64))
    value = db.Column(db.Float)
    threshold = db.Column(db.Float)
    message = db.Column(db.String(500))
    uid = db.Column(db.String(64))
    event_metadata = db.Column("metadata", db.JSON, nullable=False, default=dict)
    payload = db.Column(db.JSON, nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False, index=True)


class SensorRegistry(db.Model):
    __tablename__ = "sensor_registry"

    sensor_id = db.Column(db.String(64), primary_key=True)
    device_id = db.Column(
        db.String(64), db.ForeignKey("device.device_id"), nullable=False, index=True
    )
    sensor_type = db.Column(db.String(64), nullable=False)
    parameters = db.Column(db.JSON, nullable=False, default=list)
    unit = db.Column(db.String(32))
    sensor_metadata = db.Column("metadata", db.JSON, nullable=False, default=dict)


class ActuatorRegistry(db.Model):
    __tablename__ = "actuator_registry"

    actuator_id = db.Column(db.String(64), primary_key=True)
    device_id = db.Column(
        db.String(64), db.ForeignKey("device.device_id"), nullable=False, index=True
    )
    actuator_type = db.Column(db.String(64), nullable=False)
    supported_commands = db.Column(db.JSON, nullable=False, default=list)
    actuator_metadata = db.Column("metadata", db.JSON, nullable=False, default=dict)


class Group2Prediction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(
        db.String(64), db.ForeignKey("device.device_id"), nullable=False, index=True
    )
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    received_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    sensor_values = db.Column(db.JSON, nullable=False)
    prediction_status = db.Column(db.String(16), nullable=False, index=True)
    confidence = db.Column(db.Float, nullable=False)
    probabilities = db.Column(db.JSON, nullable=False)
    defaulted_fields = db.Column(db.JSON, nullable=False, default=list)


class DeviceCommand(db.Model):
    command_id = db.Column(db.String(36), primary_key=True)
    message_id = db.Column(db.String(36), nullable=False, unique=True)
    device_id = db.Column(
        db.String(64), db.ForeignKey("device.device_id"), nullable=False, index=True
    )
    actuator_id = db.Column(db.String(64), nullable=False)
    command = db.Column(db.String(64), nullable=False)
    parameters = db.Column(db.JSON, nullable=False)
    issued_by = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="PENDING", index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    ack_at = db.Column(db.DateTime(timezone=True))
    ack_result = db.Column(db.String(16))
    actual_state = db.Column(db.JSON)
    ack_message = db.Column(db.String(500))


class RfidCard(db.Model):
    __tablename__ = "rfid_card"

    uid = db.Column(db.String(64), primary_key=True)
    label = db.Column(db.String(120), nullable=False, default="Kartu Peternak")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
