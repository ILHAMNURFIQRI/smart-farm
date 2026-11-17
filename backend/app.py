import hmac
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from click import ClickException
from dotenv import load_dotenv
from flask_cors import CORS
from flask import Flask, jsonify, request
from werkzeug.exceptions import BadRequest, UnsupportedMediaType
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import (
    ActuatorRegistry,
    Device,
    DeviceCommand,
    FarmEvent,
    Group2Prediction,
    SensorRegistry,
    Telemetry,
    as_utc,
    db,
    utc_now,
)
from mqtt_service import MQTTService, create_command_message, next_expiry, topic_for
from group2_model import (
    FEATURES as GROUP2_FEATURES,
    Group2Model,
    Group2ModelUnavailable,
)
from schemas import (
    SEVERITIES,
    ValidationError,
    isoformat_utc,
    parse_timestamp,
    require_id,
    require_text,
    validate_stream_url,
)


load_dotenv()
COMMAND_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///smart_farm.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MQTT_ENABLED=os.getenv("MQTT_ENABLED", "false").lower() == "true",
        MQTT_HOST=os.getenv("MQTT_HOST"),
        MQTT_PORT=int(os.getenv("MQTT_PORT", "1883")),
        MQTT_USERNAME=os.getenv("MQTT_USERNAME"),
        MQTT_PASSWORD=os.getenv("MQTT_PASSWORD"),
        MQTT_TLS=os.getenv("MQTT_TLS", "false").lower() == "true",
        MQTT_CLIENT_ID=os.getenv("MQTT_CLIENT_ID", "smart-farm-backend"),
        MQTT_TOPIC_PREFIX=os.getenv("MQTT_TOPIC_PREFIX", "sf-kandang-sapih"),
        MQTT_PUBLISH_TIMEOUT=float(os.getenv("MQTT_PUBLISH_TIMEOUT", "5")),
        COMMAND_TTL_SECONDS=int(os.getenv("COMMAND_TTL_SECONDS", "30")),
        SMART_GATE_STREAM_URL=os.getenv("SMART_GATE_STREAM_URL"),
        GROUP2_MODEL_DIR=os.getenv("GROUP2_MODEL_DIR"),
        GROUP2_CORS_ORIGINS=os.getenv("GROUP2_CORS_ORIGINS", "*"),
        API_ADMIN_TOKEN=os.getenv("API_ADMIN_TOKEN"),
        API_CONTROL_TOKEN=os.getenv("API_CONTROL_TOKEN"),
        API_CONTROL_ISSUER=os.getenv("API_CONTROL_ISSUER", "configured-operator-token"),
    )
    if test_config:
        app.config.update(test_config)
    cors_origins = [
        origin.strip()
        for origin in app.config["GROUP2_CORS_ORIGINS"].split(",")
        if origin.strip()
    ]
    CORS(app, resources={
        r"/api/sensors.*": {"origins": cors_origins},
        r"/health": {"origins": cors_origins},
        r"/model/info": {"origins": cors_origins},
    })
    db.init_app(app)
    app.extensions["mqtt_service"] = None
    app.extensions["group2_model"] = None

    def get_group2_model():
        model = app.extensions["group2_model"]
        if model is None:
            model_dir = app.config.get("GROUP2_MODEL_DIR")
            if not model_dir:
                model_dir = Path(app.root_path).parent / "backend kel 2" / "model"
            model = Group2Model(model_dir)
            app.extensions["group2_model"] = model
        return model

    def parse_group2_timestamp(value):
        if value is None:
            return utc_now()
        if not isinstance(value, str):
            raise ValidationError("recorded_at must be an ISO 8601 timestamp")
        try:
            return parse_timestamp(value, "recorded_at")
        except ValidationError:
            try:
                local_time = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except ValueError as exc:
                raise ValidationError(
                    "recorded_at must be ISO 8601 or YYYY-MM-DD HH:MM:SS"
                ) from exc
            return local_time.replace(tzinfo=timezone(timedelta(hours=7))).astimezone(
                timezone.utc
            )

    def group2_prediction_json(prediction):
        return {
            "recorded_at": isoformat_utc(prediction.timestamp),
            "received_at": isoformat_utc(prediction.received_at),
            **prediction.sensor_values,
            "status": prediction.prediction_status,
            "confidence": prediction.confidence,
            "probabilitas": prediction.probabilities,
            "defaulted_fields": prediction.defaulted_fields,
        }

    @app.errorhandler(ValidationError)
    def handle_validation_error(error):
        return jsonify({"error": {"code": "VALIDATION_ERROR", "message": str(error)}}), 400

    @app.errorhandler(BadRequest)
    def handle_bad_request(_error):
        return jsonify({"error": {"code": "BAD_REQUEST", "message": "Request body contains invalid JSON"}}), 400

    @app.errorhandler(UnsupportedMediaType)
    def handle_unsupported_media_type(_error):
        return jsonify({"error": {
            "code": "UNSUPPORTED_MEDIA_TYPE",
            "message": "Content-Type must be application/json",
        }}), 415

    def require_token(config_key):
        configured = app.config.get(config_key)
        if not configured:
            return jsonify({
                "error": {
                    "code": "AUTH_NOT_CONFIGURED",
                    "message": f"{config_key} must be configured before this operation is enabled",
                }
            }), 503
        authorization = request.headers.get("Authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, configured):
            return jsonify({"error": {"code": "UNAUTHORIZED", "message": "Valid bearer token required"}}), 401
        return None

    def get_json_object():
        value = request.get_json(silent=False)
        if not isinstance(value, dict):
            raise ValidationError("request body must be a JSON object")
        return value

    def device_json(device):
        return {
            "device_id": device.device_id,
            "farm_id": device.farm_id,
            "barn_id": device.barn_id,
            "zone_id": device.zone_id,
            "device_type": device.device_type,
            "device_name": device.device_name,
            "stream_url": device.stream_url,
            "connection_status": device.connection_status,
            "operational_state": device.operational_state,
            "firmware_version": device.firmware_version,
            "uptime_seconds": device.uptime_seconds,
            "last_seen_at": isoformat_utc(device.last_seen_at) if device.last_seen_at else None,
        }

    def pagination():
        try:
            page = int(request.args.get("page", "1"))
            per_page = int(request.args.get("per_page", "50"))
        except ValueError as exc:
            raise ValidationError("page and per_page must be integers") from exc
        if page < 1 or per_page < 1 or per_page > 100:
            raise ValidationError("page must be positive and per_page must be between 1 and 100")
        return page, per_page

    @app.post("/api/sensors")
    def receive_group2_sensor():
        body = get_json_object()
        device = db.session.get(Device, "DEV_002")
        if device is None:
            return jsonify({
                "error": {
                    "code": "DEVICE_NOT_SEEDED",
                    "message": "Run `flask --app app seed-dev002` before sending Group 2 data",
                }
            }), 409

        sensor_ids = {
            "temperature_c": "SNS_TEMP_002",
            "humidity_rh": "SNS_HUMIDITY_002",
            "sound_rms": "SNS_SOUND_002",
            "mq135_raw": "SNS_MQ135_002",
        }
        for sensor_id in sensor_ids.values():
            sensor = db.session.get(SensorRegistry, sensor_id)
            if sensor is None or sensor.device_id != device.device_id:
                return jsonify({
                    "error": {
                        "code": "SENSOR_REGISTRY_INCOMPLETE",
                        "message": f"{sensor_id} is not registered for DEV_002",
                    }
                }), 409

        values = {}
        defaulted_fields = []
        for feature in GROUP2_FEATURES:
            if feature not in body and feature in {"sound_rms", "mq135_raw"}:
                values[feature] = 0.0
                defaulted_fields.append(feature)
                continue
            value = body.get(feature)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValidationError(f"{feature} must be a finite number")
            values[feature] = float(value)
        if not 0 <= values["humidity_rh"] <= 100:
            raise ValidationError("humidity_rh must be between 0 and 100")
        for feature in ("sound_rms", "mq135_raw"):
            if values[feature] < 0:
                raise ValidationError(f"{feature} must be non-negative")
        recorded_at = parse_group2_timestamp(body.get("recorded_at"))

        try:
            predictor = get_group2_model()
            prediction_result = predictor.predict(values)
        except Group2ModelUnavailable as exc:
            app.logger.error("Group 2 model unavailable: %s", exc)
            return jsonify({
                "error": {
                    "code": "MODEL_UNAVAILABLE",
                    "message": "Group 2 model is not configured or could not be loaded",
                }
            }), 503

        received_at = utc_now()
        message_id = str(uuid4())
        units = {
            "temperature_c": "degC",
            "humidity_rh": "%RH",
            "sound_rms": "raw",
            "mq135_raw": "raw",
        }
        quality = {
            feature: "UNCERTAIN" if feature in defaulted_fields else "GOOD"
            for feature in GROUP2_FEATURES
        }
        for feature, sensor_id in sensor_ids.items():
            db.session.add(Telemetry(
                message_id=message_id,
                device_id=device.device_id,
                sensor_id=sensor_id,
                parameter=feature,
                value=values[feature],
                unit=units[feature],
                quality=quality[feature],
                timestamp=recorded_at,
            ))
        db.session.add(Group2Prediction(
            device_id=device.device_id,
            timestamp=recorded_at,
            received_at=received_at,
            sensor_values=values,
            prediction_status=prediction_result["status"],
            confidence=prediction_result["confidence"],
            probabilities=prediction_result["probabilitas"],
            defaulted_fields=defaulted_fields,
        ))
        device.connection_status = "ONLINE"
        device.last_seen_at = received_at
        db.session.commit()
        return jsonify({
            "message": "Data diterima",
            "device_id": device.device_id,
            "recorded_at": isoformat_utc(recorded_at),
            **prediction_result,
            "defaulted_fields": defaulted_fields,
        }), 201

    @app.get("/api/sensors/latest")
    def get_group2_latest():
        latest = db.session.scalars(
            db.select(Group2Prediction)
            .where(Group2Prediction.device_id == "DEV_002")
            .order_by(Group2Prediction.id.desc())
            .limit(1)
        ).first()
        if latest is None:
            return jsonify({"message": "Belum ada data dari sensor"}), 204
        return jsonify(group2_prediction_json(latest))

    @app.get("/api/sensors/history")
    def get_group2_history():
        rows = db.session.scalars(
            db.select(Group2Prediction)
            .where(Group2Prediction.device_id == "DEV_002")
            .order_by(Group2Prediction.id.desc())
            .limit(50)
        ).all()
        rows.reverse()
        return jsonify({
            "total": len(rows),
            "data": [group2_prediction_json(row) for row in rows],
        })

    @app.get("/health")
    def group2_health():
        try:
            predictor = get_group2_model()
        except Group2ModelUnavailable as exc:
            app.logger.error("Group 2 model unavailable: %s", exc)
            return jsonify({
                "status": "error",
                "error": {
                    "code": "MODEL_UNAVAILABLE",
                    "message": "Group 2 model is not configured or could not be loaded",
                },
            }), 503
        return jsonify({
            "status": "ok",
            "model": predictor.metadata["model"],
            "accuracy": predictor.metadata["accuracy"],
            "classes": predictor.metadata["classes"],
            "data_received": db.session.scalar(
                db.select(func.count()).select_from(Group2Prediction)
                .where(Group2Prediction.device_id == "DEV_002")
            ) or 0,
        })

    @app.get("/model/info")
    def group2_model_info():
        try:
            predictor = get_group2_model()
        except Group2ModelUnavailable as exc:
            app.logger.error("Group 2 model unavailable: %s", exc)
            return jsonify({
                "error": {
                    "code": "MODEL_UNAVAILABLE",
                    "message": "Group 2 model is not configured or could not be loaded",
                }
            }), 503
        return jsonify(predictor.metadata)

    @app.get("/api/v1/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/api/v1/devices")
    def list_devices():
        devices = db.session.scalars(db.select(Device).order_by(Device.device_id)).all()
        return jsonify({"items": [device_json(device) for device in devices], "total": len(devices)})

    @app.post("/api/v1/devices")
    def register_device():
        auth_error = require_token("API_ADMIN_TOKEN")
        if auth_error:
            return auth_error
        body = get_json_object()
        device_id = require_id(body.get("device_id"), "device_id")
        farm_id = require_id(body.get("farm_id"), "farm_id")
        barn_id = require_id(body.get("barn_id"), "barn_id")
        zone_id = body.get("zone_id")
        if zone_id is not None:
            zone_id = require_id(zone_id, "zone_id")
        device = Device(
            device_id=device_id,
            farm_id=farm_id,
            barn_id=barn_id,
            zone_id=zone_id,
            device_type=require_text(body["device_type"], "device_type", 64)
            if body.get("device_type") is not None else None,
            device_name=require_text(body["device_name"], "device_name", 120)
            if body.get("device_name") is not None else None,
            stream_url=validate_stream_url(body.get("stream_url")),
            config={},
        )
        db.session.add(device)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"error": {"code": "DEVICE_EXISTS", "message": "device_id is already registered"}}), 409
        return jsonify({"item": device_json(device)}), 201

    @app.get("/api/v1/devices/<device_id>")
    def get_device(device_id):
        device = db.session.get(Device, device_id)
        if device is None:
            return jsonify({"error": {"code": "NOT_FOUND", "message": "Device not found"}}), 404
        return jsonify({"item": device_json(device)})

    @app.get("/api/v1/telemetry")
    def list_telemetry():
        device_id = request.args.get("device_id")
        if not device_id:
            raise ValidationError("device_id is required")
        if db.session.get(Device, device_id) is None:
            return jsonify({"error": {"code": "NOT_FOUND", "message": "Device not found"}}), 404
        page, per_page = pagination()
        query = db.select(Telemetry).where(Telemetry.device_id == device_id)
        start = request.args.get("from")
        end = request.args.get("to")
        if start:
            query = query.where(Telemetry.timestamp >= parse_timestamp(start, "from"))
        if end:
            query = query.where(Telemetry.timestamp <= parse_timestamp(end, "to"))
        total = db.session.scalar(db.select(func.count()).select_from(query.subquery())) or 0
        rows = db.session.scalars(
            query.order_by(Telemetry.timestamp.desc(), Telemetry.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
        return jsonify({
            "items": [
                {
                    "device_id": row.device_id,
                    "sensor_id": row.sensor_id,
                    "parameter": row.parameter,
                    "value": row.value,
                    "unit": row.unit,
                    "quality": row.quality,
                    "message_id": row.message_id,
                    "timestamp": isoformat_utc(row.timestamp),
                }
                for row in rows
            ],
            "page": page,
            "per_page": per_page,
            "total": total,
        })

    @app.get("/api/v1/events")
    def list_events():
        page, per_page = pagination()
        query = db.select(FarmEvent)
        severity = request.args.get("severity")
        if severity:
            severity = severity.upper()
            if severity not in SEVERITIES:
                raise ValidationError("severity must be INFO, WARNING, HIGH, or CRITICAL")
            query = query.where(FarmEvent.severity == severity)
        device_id = request.args.get("device_id")
        if device_id:
            query = query.where(FarmEvent.device_id == device_id)
        total = db.session.scalar(db.select(func.count()).select_from(query.subquery())) or 0
        rows = db.session.scalars(
            query.order_by(FarmEvent.timestamp.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
        return jsonify({
            "items": [
                {
                    "event_id": row.event_id,
                    "device_id": row.device_id,
                    "event_type": row.event_type,
                    "severity": row.severity,
                    "state": row.state,
                    "uid": row.uid,
                    "metadata": row.event_metadata,
                    "parameter": row.parameter,
                    "value": row.value,
                    "threshold": row.threshold,
                    "message": row.message,
                    "timestamp": isoformat_utc(row.timestamp),
                }
                for row in rows
            ],
            "page": page,
            "per_page": per_page,
            "total": total,
        })

    @app.get("/api/v1/dashboard/summary")
    def dashboard_summary():
        latest_telemetry_at = db.session.scalar(db.select(func.max(Telemetry.timestamp)))
        return jsonify({
            "device_count": db.session.scalar(db.select(func.count()).select_from(Device)) or 0,
            "online_device_count": db.session.scalar(
                db.select(func.count()).select_from(Device).where(Device.connection_status == "ONLINE")
            ) or 0,
            "active_event_count": db.session.scalar(
                db.select(func.count()).select_from(FarmEvent).where(FarmEvent.state == "ACTIVE")
            ) or 0,
            "latest_telemetry_at": (
                isoformat_utc(latest_telemetry_at) if latest_telemetry_at else None
            ),
        })

    @app.get("/api/v1/config/devices/<device_id>")
    def get_device_config(device_id):
        device = db.session.get(Device, device_id)
        if device is None:
            return jsonify({"error": {"code": "NOT_FOUND", "message": "Device not found"}}), 404
        return jsonify({"device_id": device.device_id, "config": device.config})

    @app.post("/api/v1/devices/<device_id>/commands")
    def issue_command(device_id):
        auth_error = require_token("API_CONTROL_TOKEN")
        if auth_error:
            return auth_error
        device = db.session.get(Device, device_id)
        if device is None:
            return jsonify({"error": {"code": "NOT_FOUND", "message": "Device not found"}}), 404
        if device.connection_status != "ONLINE":
            return jsonify({"error": {"code": "DEVICE_OFFLINE", "message": "Commands require an ONLINE device"}}), 409
        body = get_json_object()
        if body.get("device_id") not in (None, device_id):
            raise ValidationError("device_id in the body must match the URL")
        parameters = body.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValidationError("parameters must be a JSON object")
        actuator_id = body.get("actuator_id", parameters.get("actuator"))
        actuator_id = require_id(actuator_id, "actuator_id")
        command_name = require_text(body.get("command"), "command", 64)
        if not COMMAND_PATTERN.fullmatch(command_name):
            raise ValidationError("command must use uppercase letters, digits, and underscores")
        if device_id in {"DEV_003", "DEV_004"}:
            actuator = db.session.get(ActuatorRegistry, actuator_id)
            if actuator is None or actuator.device_id != device_id:
                raise ValidationError(f"{actuator_id} is not registered for {device_id}")
            if command_name not in actuator.supported_commands:
                raise ValidationError(
                    f"{command_name} is not supported by actuator {actuator_id}"
                )
        if device_id == "DEV_003":
            state = parameters.get("state")
            if (
                command_name != "SET_STATE"
                or not isinstance(state, str)
                or state not in {"ON", "OFF"}
            ):
                raise ValidationError("DEV_003 actuators require SET_STATE with state ON or OFF")
            if actuator_id == "ACT_PUMP_003" and set(parameters) != {"state"}:
                raise ValidationError("ACT_PUMP_003 accepts only the state parameter")
            if actuator_id == "ACT_FEEDER_MOTOR_003":
                if set(parameters) - {"state", "speed_percent"}:
                    raise ValidationError(
                        "ACT_FEEDER_MOTOR_003 accepts state and optional speed_percent"
                    )
                speed = parameters.get("speed_percent")
                if speed is not None and (
                    isinstance(speed, bool)
                    or not isinstance(speed, int)
                    or not 0 <= speed <= 100
                ):
                    raise ValidationError("speed_percent must be an integer from 0 to 100")

        service = app.extensions.get("mqtt_service")
        command_id = str(uuid4())
        now = utc_now()
        record = DeviceCommand(
            command_id=command_id,
            message_id=str(uuid4()),
            device_id=device.device_id,
            actuator_id=actuator_id,
            command=command_name,
            parameters=parameters,
            issued_by=app.config["API_CONTROL_ISSUER"],
            status="PENDING",
            created_at=now,
            expires_at=next_expiry(app),
        )
        db.session.add(record)
        db.session.commit()
        if service is None:
            record.status = "FAILED"
            db.session.commit()
            return jsonify({"error": {
                "code": "MQTT_UNAVAILABLE",
                "message": "MQTT is disabled or not initialized; command was not published",
                "command_id": command_id,
            }}), 503
        try:
            service.publish(
                topic_for(device, "command", app.config["MQTT_TOPIC_PREFIX"]),
                create_command_message(device, record),
            )
        except (RuntimeError, TimeoutError) as error:
            record.status = "FAILED"
            db.session.commit()
            app.logger.error("Command publish failed command_id=%s device_id=%s: %s", command_id, device_id, error)
            return jsonify({"error": {
                "code": "MQTT_PUBLISH_FAILED",
                "message": "Command was recorded but could not be published",
                "command_id": command_id,
            }}), 503
        return jsonify({
            "command_id": command_id,
            "device_id": device_id,
            "status": record.status,
            "expires_at": isoformat_utc(record.expires_at),
        }), 202

    @app.get("/api/v1/commands/<command_id>")
    def get_command(command_id):
        command = db.session.get(DeviceCommand, command_id)
        if command is None:
            return jsonify({"error": {"code": "NOT_FOUND", "message": "Command not found"}}), 404
        if command.status == "PENDING" and utc_now() >= as_utc(command.expires_at):
            command.status = "EXPIRED"
            db.session.commit()
        return jsonify({
            "command_id": command.command_id,
            "device_id": command.device_id,
            "actuator_id": command.actuator_id,
            "command": command.command,
            "parameters": command.parameters,
            "status": command.status,
            "created_at": isoformat_utc(command.created_at),
            "expires_at": isoformat_utc(command.expires_at),
            "ack_at": isoformat_utc(command.ack_at) if command.ack_at else None,
            "ack_result": command.ack_result,
            "actual_state": command.actual_state,
            "message": command.ack_message,
        })

    @app.cli.command("init-db")
    def init_db_command():
        """Create tables for a new local database."""
        with app.app_context():
            db.create_all()
        print("Database tables created.")

    @app.cli.command("seed-dev004")
    def seed_dev004_command():
        """Create or update the DEV_004 device, sensor, and actuator registry."""
        with app.app_context():
            db.create_all()
            stream_url = validate_stream_url(app.config["SMART_GATE_STREAM_URL"])
            device = db.session.get(Device, "DEV_004")
            if device is None:
                device = Device(
                    device_id="DEV_004",
                    farm_id="FARM_001",
                    barn_id="BARN_001",
                    zone_id="ZONE_001",
                    device_type="SMART_GATE",
                    device_name="Smart Gate & Internal Surveillance",
                    connection_status="UNKNOWN",
                    config={},
                )
                db.session.add(device)
            elif (
                device.farm_id != "FARM_001"
                or device.barn_id != "BARN_001"
                or device.zone_id != "ZONE_001"
            ):
                raise ClickException("DEV_004 already exists with a different farm/barn/zone")
            if stream_url:
                device.stream_url = stream_url

            sensors = (
                SensorRegistry(
                    sensor_id="SNS_RFID_001",
                    device_id="DEV_004",
                    sensor_type="RFID_READER",
                    parameters=["rfid_uid", "gate_state", "alarm_state"],
                    unit="state",
                    sensor_metadata={"reader": "MFRC522", "controller": "ESP32"},
                ),
                SensorRegistry(
                    sensor_id="SNS_CAM_AI_001",
                    device_id="DEV_004",
                    sensor_type="AI_DETECTOR",
                    parameters=[
                        "cow_count",
                        "farmer_count",
                        "intruder_count",
                        "animal_anomaly_count",
                    ],
                    unit="count",
                    sensor_metadata={"model": "YOLOv8", "processor": "laptop-edge-gateway"},
                ),
            )
            for sensor in sensors:
                existing = db.session.get(SensorRegistry, sensor.sensor_id)
                if existing and existing.device_id != "DEV_004":
                    raise ClickException(f"{sensor.sensor_id} is already assigned to another device")
                if existing:
                    existing.sensor_type = sensor.sensor_type
                    existing.parameters = sensor.parameters
                    existing.unit = sensor.unit
                    existing.sensor_metadata = sensor.sensor_metadata
                else:
                    db.session.add(sensor)

            actuators = (
                ActuatorRegistry(
                    actuator_id="ACT_GATE_001",
                    device_id="DEV_004",
                    actuator_type="GATE_SERVO",
                    supported_commands=["OPEN_GATE", "CLOSE_GATE"],
                    actuator_metadata={"controller": "ESP32"},
                ),
                ActuatorRegistry(
                    actuator_id="ACT_ALARM_001",
                    device_id="DEV_004",
                    actuator_type="OPERATOR_ALARM",
                    supported_commands=["ALERT", "CLEAR"],
                    actuator_metadata={"outputs": ["buzzer", "led"]},
                ),
            )
            for actuator in actuators:
                existing = db.session.get(ActuatorRegistry, actuator.actuator_id)
                if existing and existing.device_id != "DEV_004":
                    raise ClickException(
                        f"{actuator.actuator_id} is already assigned to another device"
                    )
                if existing:
                    existing.actuator_type = actuator.actuator_type
                    existing.supported_commands = actuator.supported_commands
                    existing.actuator_metadata = actuator.actuator_metadata
                else:
                    db.session.add(actuator)
            db.session.commit()
        print("DEV_004, its sensors, and actuators are registered.")

    @app.cli.command("seed-dev001")
    def seed_dev001_command():
        """Create or update the Group 1 smart-collar pulse sensor registry."""
        with app.app_context():
            db.create_all()
            device = db.session.get(Device, "DEV_001")
            if device is None:
                device = Device(
                    device_id="DEV_001",
                    farm_id="FARM_001",
                    barn_id="BARN_001",
                    zone_id="ZONE_001",
                    device_type="SMART_COLLAR",
                    device_name="Smart Cattle Collar",
                    connection_status="UNKNOWN",
                    config={},
                )
                db.session.add(device)
            elif (
                device.farm_id != "FARM_001"
                or device.barn_id != "BARN_001"
                or device.zone_id != "ZONE_001"
            ):
                raise ClickException("DEV_001 already exists with a different farm/barn/zone")
            else:
                device.device_type = "SMART_COLLAR"
                device.device_name = "Smart Cattle Collar"

            sensor = db.session.get(SensorRegistry, "SNS_PULSE_001")
            if sensor and sensor.device_id != "DEV_001":
                raise ClickException("SNS_PULSE_001 is already assigned to another device")
            if sensor:
                sensor.sensor_type = "PULSE_ANALOG"
                sensor.parameters = ["pulse_raw", "heart_rate_bpm"]
                sensor.unit = None
                sensor.sensor_metadata = {
                    "signal_parameter": "pulse_raw",
                    "signal_unit": "adc",
                    "heart_rate_parameter": "heart_rate_bpm",
                    "heart_rate_unit": "bpm",
                }
            else:
                db.session.add(SensorRegistry(
                    sensor_id="SNS_PULSE_001",
                    device_id="DEV_001",
                    sensor_type="PULSE_ANALOG",
                    parameters=["pulse_raw", "heart_rate_bpm"],
                    unit=None,
                    sensor_metadata={
                        "signal_parameter": "pulse_raw",
                        "signal_unit": "adc",
                        "heart_rate_parameter": "heart_rate_bpm",
                        "heart_rate_unit": "bpm",
                    },
                ))
            db.session.commit()
        print("DEV_001 and its analog pulse sensor are registered.")

    @app.cli.command("seed-dev002")
    def seed_dev002_command():
        """Create or update the Group 2 sensor device registry."""
        with app.app_context():
            db.create_all()
            device = db.session.get(Device, "DEV_002")
            if device is None:
                device = Device(
                    device_id="DEV_002",
                    farm_id="FARM_001",
                    barn_id="BARN_001",
                    zone_id="ZONE_001",
                    device_type="BARN_MONITORING_ML",
                    device_name="Moo-nitoring Kandang Sapi",
                    connection_status="UNKNOWN",
                    config={},
                )
                db.session.add(device)
            elif (
                device.farm_id != "FARM_001"
                or device.barn_id != "BARN_001"
                or device.zone_id != "ZONE_001"
            ):
                raise ClickException("DEV_002 already exists with a different farm/barn/zone")
            else:
                device.device_type = "BARN_MONITORING_ML"
                device.device_name = "Moo-nitoring Kandang Sapi"

            sensor_specs = (
                ("SNS_TEMP_002", "TEMPERATURE", "temperature_c", "degC"),
                ("SNS_HUMIDITY_002", "HUMIDITY", "humidity_rh", "%RH"),
                ("SNS_SOUND_002", "SOUND_LEVEL", "sound_rms", "raw"),
                ("SNS_MQ135_002", "AIR_QUALITY_MQ135", "mq135_raw", "raw"),
            )
            for sensor_id, sensor_type, parameter, unit in sensor_specs:
                existing = db.session.get(SensorRegistry, sensor_id)
                if existing and existing.device_id != "DEV_002":
                    raise ClickException(f"{sensor_id} is already assigned to another device")
                if existing:
                    existing.sensor_type = sensor_type
                    existing.parameters = [parameter]
                    existing.unit = unit
                    existing.sensor_metadata = {"group": 2}
                else:
                    db.session.add(SensorRegistry(
                        sensor_id=sensor_id,
                        device_id="DEV_002",
                        sensor_type=sensor_type,
                        parameters=[parameter],
                        unit=unit,
                        sensor_metadata={"group": 2},
                    ))
            db.session.commit()
        print("DEV_002 and its sensors are registered.")

    @app.cli.command("seed-dev003")
    def seed_dev003_command():
        """Create or update the Group 3 feeding device registry."""
        with app.app_context():
            db.create_all()
            device = db.session.get(Device, "DEV_003")
            if device is None:
                device = Device(
                    device_id="DEV_003",
                    farm_id="FARM_001",
                    barn_id="BARN_001",
                    zone_id="ZONE_001",
                    device_type="SMART_FEEDING",
                    device_name="Smart Feeding and Water Level",
                    connection_status="UNKNOWN",
                    config={},
                )
                db.session.add(device)
            elif (
                device.farm_id != "FARM_001"
                or device.barn_id != "BARN_001"
                or device.zone_id != "ZONE_001"
            ):
                raise ClickException("DEV_003 already exists with a different farm/barn/zone")
            else:
                device.device_type = "SMART_FEEDING"
                device.device_name = "Smart Feeding and Water Level"

            sensor_specs = (
                (
                    "SNS_ULTRASONIC_003",
                    "ULTRASONIC_HCSR04",
                    ["distance_cm", "feed_level_percent", "feed_status"],
                    "cm",
                ),
                (
                    "SNS_WATER_LEVEL_003",
                    "WATER_LEVEL",
                    ["water_level_percent"],
                    "%",
                ),
            )
            for sensor_id, sensor_type, parameters, unit in sensor_specs:
                existing = db.session.get(SensorRegistry, sensor_id)
                if existing and existing.device_id != "DEV_003":
                    raise ClickException(f"{sensor_id} is already assigned to another device")
                if existing:
                    existing.sensor_type = sensor_type
                    existing.parameters = parameters
                    existing.unit = unit
                    existing.sensor_metadata = {"group": 3}
                else:
                    db.session.add(SensorRegistry(
                        sensor_id=sensor_id,
                        device_id="DEV_003",
                        sensor_type=sensor_type,
                        parameters=parameters,
                        unit=unit,
                        sensor_metadata={"group": 3},
                    ))

            actuator_specs = (
                (
                    "ACT_PUMP_003",
                    "WATER_PUMP",
                    ["SET_STATE"],
                    {"control": "single-pump"},
                ),
                (
                    "ACT_FEEDER_MOTOR_003",
                    "DC_MOTOR_L298N",
                    ["SET_STATE"],
                    {"driver": "L298N", "speed_parameter": "speed_percent"},
                ),
            )
            for actuator_id, actuator_type, commands, metadata in actuator_specs:
                existing = db.session.get(ActuatorRegistry, actuator_id)
                if existing and existing.device_id != "DEV_003":
                    raise ClickException(
                        f"{actuator_id} is already assigned to another device"
                    )
                if existing:
                    existing.actuator_type = actuator_type
                    existing.supported_commands = commands
                    existing.actuator_metadata = metadata
                else:
                    db.session.add(ActuatorRegistry(
                        actuator_id=actuator_id,
                        device_id="DEV_003",
                        actuator_type=actuator_type,
                        supported_commands=commands,
                        actuator_metadata=metadata,
                    ))
            db.session.commit()
        print("DEV_003, its sensors, and actuators are registered.")

    return app


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if app.config["MQTT_ENABLED"]:
        service = MQTTService(app)
        service.start()
        app.extensions["mqtt_service"] = service
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
