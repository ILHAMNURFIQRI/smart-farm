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
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest, UnsupportedMediaType
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import (
    ActuatorRegistry,
    BarnRecoveryLog,
    Device,
    DeviceCommand,
    FarmEvent,
    Group2Prediction,
    SanitationLog,
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
from moo_nitoring import (
    calculate_comfort_metrics,
    calculate_thi,
    get_active_recovery_session,
    get_group2_model,
    get_kandang_early_warning,
    group2_prediction_json,
    moo_nitoring_bp,
    parse_group2_timestamp,
    recovery_log_json,
    update_recovery_sessions,
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
        r"/api/*": {"origins": cors_origins},
        r"/health": {"origins": cors_origins},
        r"/model/info": {"origins": cors_origins},
    })
    db.init_app(app)
    app.extensions["mqtt_service"] = None
    app.extensions["group2_model"] = None
    app.register_blueprint(moo_nitoring_bp)

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
            "total_ternak": 24,
            "pakan_silo_kg": 203,
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
        if device_id in {"DEV_002", "DEV_003", "DEV_004"}:
            actuator = db.session.get(ActuatorRegistry, actuator_id)
            if actuator is None or actuator.device_id != device_id:
                raise ValidationError(f"{actuator_id} is not registered for {device_id}")
            if command_name not in actuator.supported_commands:
                raise ValidationError(
                    f"{command_name} is not supported by actuator {actuator_id}"
                )
        if device_id == "DEV_002":
            if actuator_id in {"ACT_FAN_002", "ACT_LED_002"}:
                state = parameters.get("state")
                if (
                    command_name != "SET_STATE"
                    or not isinstance(state, str)
                    or state not in {"ON", "OFF"}
                ):
                    raise ValidationError(f"{actuator_id} requires SET_STATE with state ON or OFF")
                if actuator_id == "ACT_FAN_002":
                    if state == "ON":
                        active_sess = get_active_recovery_session("DEV_002")
                        if active_sess is None:
                            latest_pred = db.session.scalars(
                                db.select(Group2Prediction)
                                .where(Group2Prediction.device_id == "DEV_002")
                                .order_by(Group2Prediction.id.desc())
                                .limit(1)
                            ).first()
                            init_temp = float(latest_pred.sensor_values.get("temperature_c", 30.0)) if latest_pred else 30.0
                            db.session.add(BarnRecoveryLog(
                                device_id="DEV_002",
                                actuator_id="ACT_FAN_002",
                                start_time=utc_now(),
                                initial_temperature=init_temp,
                                target_temperature=29.5,
                                evaluation_minutes=15,
                                status="IN_PROGRESS",
                                recommendation="Kipas aktif. Mengevaluasi respons pemulihan mikroklimat.",
                            ))
                    elif state == "OFF":
                        active_sess = get_active_recovery_session("DEV_002")
                        if active_sess:
                            active_sess.status = "MANUAL_STOP"
                            active_sess.end_time = utc_now()
                            active_sess.duration_minutes = round(
                                (as_utc(active_sess.end_time) - as_utc(active_sess.start_time)).total_seconds() / 60.0, 1
                            )
                            active_sess.recommendation = "Kipas dimatikan secara manual oleh operator."
            elif actuator_id == "ACT_BUZZER_002":
                if command_name not in {"ALERT", "CLEAR"}:
                    raise ValidationError("ACT_BUZZER_002 requires command ALERT or CLEAR")
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
            if device_id == "DEV_003" and hasattr(service, "client") and hasattr(service.client, "publish"):
                try:
                    state_val = parameters.get("state", "OFF")
                    if actuator_id == "ACT_PUMP_003":
                        service.client.publish("smartfeeding/pump/command", state_val, qos=1)
                    elif actuator_id == "ACT_FEEDER_MOTOR_003":
                        service.client.publish("smartfeeding/conveyor/command", state_val, qos=1)
                        if "speed_percent" in parameters:
                            speed_pwm = int(parameters["speed_percent"] * 255 / 100)
                            service.client.publish("smartfeeding/conveyor/speed", str(speed_pwm), qos=1)
                except Exception as leg_err:
                    app.logger.warning("Legacy publish ignored: %s", leg_err)
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

    @app.get("/")
    @app.get("/dashboard")
    def serve_frontend_dashboard():
        frontend_dir = Path(app.root_path).parent / "frontend"
        return send_from_directory(frontend_dir, "dashboard_smart_farm.html")

    @app.get("/moo-nitoring")
    def serve_frontend_moo_nitoring():
        frontend_dir = Path(app.root_path).parent / "frontend"
        moo_file = frontend_dir / "moo_nitoring.html"
        if moo_file.is_file():
            return send_from_directory(frontend_dir, "moo_nitoring.html")
        return send_from_directory(frontend_dir, "dashboard_smart_farm.html")

    @app.get("/css/<path:filename>")
    def serve_frontend_css(filename):
        css_dir = Path(app.root_path).parent / "frontend" / "css"
        return send_from_directory(css_dir, filename)

    @app.get("/img/<path:filename>")
    def serve_frontend_img(filename):
        img_dir = Path(app.root_path).parent / "frontend" / "img"
        return send_from_directory(img_dir, filename)

    @app.get("/js/<path:filename>")
    def serve_frontend_js(filename):
        js_dir = Path(app.root_path).parent / "frontend" / "js"
        return send_from_directory(js_dir, filename)

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

            actuator_specs = (
                (
                    "ACT_FAN_002",
                    "RELAY_FAN_5V",
                    ["SET_STATE"],
                    {"controller": "ESP32", "pin": "GPIO26", "function": "Mini Fan 5V Cooling"},
                ),
                (
                    "ACT_LED_002",
                    "LIGHTING_CIRCADIAN",
                    ["SET_STATE"],
                    {"controller": "ESP32", "pin": "GPIO27", "schedule": "RTC DS3231"},
                ),
                (
                    "ACT_BUZZER_002",
                    "AUDIBLE_ALARM",
                    ["ALERT", "CLEAR"],
                    {"controller": "ESP32", "pin": "GPIO14", "type": "Active Buzzer"},
                ),
            )
            for actuator_id, actuator_type, commands, metadata in actuator_specs:
                existing = db.session.get(ActuatorRegistry, actuator_id)
                if existing and existing.device_id != "DEV_002":
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
                        device_id="DEV_002",
                        actuator_type=actuator_type,
                        supported_commands=commands,
                        actuator_metadata=metadata,
                    ))

            db.session.commit()
        print("DEV_002, its sensors, and actuators are registered.")

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
