import unittest
from datetime import datetime, timezone

from app import create_app
from models import (
    ActuatorRegistry,
    Device,
    DeviceCommand,
    FarmEvent,
    SensorRegistry,
    Telemetry,
    db,
)
from mqtt_service import parse_topic, process_incoming_message
from schemas import ValidationError, validate_envelope


class FakeGroup2Model:
    metadata = {
        "model": "RandomForestClassifier",
        "features": ["temperature_c", "humidity_rh", "sound_rms", "mq135_raw"],
        "classes": ["Kritis", "Normal", "Warning"],
        "accuracy": 0.99,
    }

    def predict(self, values):
        self.values = values
        return {
            "status": "Warning",
            "confidence": 92.5,
            "probabilitas": {"Kritis": 2.0, "Normal": 5.5, "Warning": 92.5},
        }


class FakeMQTTService:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=1):
        self.published.append((topic, payload, qos))


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "API_ADMIN_TOKEN": "admin-test-token",
            "API_CONTROL_TOKEN": None,
            "SMART_GATE_STREAM_URL": None,
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add(Device(
            device_id="DEV_001",
            farm_id="FARM_001",
            barn_id="BARN_001",
            zone_id="ZONE_001",
            connection_status="ONLINE",
            config={},
        ))
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_device_registration_requires_admin_token(self):
        body = {
            "device_id": "DEV_002",
            "farm_id": "FARM_001",
            "barn_id": "BARN_001",
        }
        denied = self.client.post("/api/v1/devices", json=body)
        self.assertEqual(denied.status_code, 401)
        accepted = self.client.post(
            "/api/v1/devices",
            json=body,
            headers={"Authorization": "Bearer admin-test-token"},
        )
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(accepted.json["item"]["device_id"], "DEV_002")

    def test_telemetry_is_validated_stored_and_deduplicated(self):
        message = {
            "schema_version": "1.0.0",
            "message_id": "550e8400-e29b-41d4-a716-446655440000",
            "message_type": "telemetry",
            "timestamp": "2026-11-17T08:00:00Z",
            "farm_id": "FARM_001",
            "barn_id": "BARN_001",
            "zone_id": "ZONE_001",
            "device_id": "DEV_001",
            "payload": {
                "readings": [{
                    "sensor_id": "SNS_TEMP_001",
                    "parameter": "temperature",
                    "value": 29.5,
                    "unit": "degC",
                    "quality": "GOOD",
                }]
            },
        }
        topic = "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_001/telemetry"
        self.assertTrue(process_incoming_message(topic, message))
        self.assertFalse(process_incoming_message(topic, message))
        self.assertEqual(db.session.query(Telemetry).count(), 1)
        history = self.client.get("/api/v1/telemetry?device_id=DEV_001")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json["items"][0]["value"], 29.5)

    def test_group1_pulse_sensor_seed_and_telemetry_support_adc_and_heart_rate(self):
        first_seed = self.app.test_cli_runner().invoke(args=["seed-dev001"])
        self.assertIsNone(first_seed.exception, first_seed.output)
        repeated_seed = self.app.test_cli_runner().invoke(args=["seed-dev001"])
        self.assertIsNone(repeated_seed.exception, repeated_seed.output)

        sensor = db.session.get(SensorRegistry, "SNS_PULSE_001")
        self.assertEqual(sensor.device_id, "DEV_001")
        self.assertEqual(sensor.parameters, ["pulse_raw", "heart_rate_bpm"])

        message = {
            "schema_version": "1.0.0",
            "message_id": "550e8400-e29b-41d4-a716-446655440040",
            "message_type": "telemetry",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "farm_id": "FARM_001",
            "barn_id": "BARN_001",
            "zone_id": "ZONE_001",
            "device_id": "DEV_001",
            "payload": {
                "readings": [
                    {
                        "sensor_id": "SNS_PULSE_001",
                        "parameter": "pulse_raw",
                        "value": 2048,
                        "unit": "adc",
                    },
                    {
                        "sensor_id": "SNS_PULSE_001",
                        "parameter": "heart_rate_bpm",
                        "value": 72,
                        "unit": "bpm",
                    },
                ]
            },
        }
        topic = "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_001/telemetry"
        self.assertTrue(process_incoming_message(topic, message))

        wrong_sensor = dict(message)
        wrong_sensor["message_id"] = "550e8400-e29b-41d4-a716-446655440041"
        wrong_sensor["payload"] = {"readings": [{
            "sensor_id": "SNS_UNKNOWN_001",
            "parameter": "heart_rate_bpm",
            "value": 72,
            "unit": "bpm",
        }]}
        with self.assertRaises(ValidationError):
            process_incoming_message(topic, wrong_sensor)
        db.session.rollback()

        for parameter, invalid_value in (("pulse_raw", 4096), ("heart_rate_bpm", 301)):
            invalid = dict(message)
            invalid["payload"] = {"readings": [{
                "sensor_id": "SNS_PULSE_001",
                "parameter": parameter,
                "value": invalid_value,
                "unit": "adc" if parameter == "pulse_raw" else "bpm",
            }]}
            with self.assertRaises(ValidationError):
                validate_envelope(invalid, "telemetry")

    def test_rejects_topic_envelope_identity_mismatch(self):
        message = {
            "schema_version": "1.0.0",
            "message_id": "550e8400-e29b-41d4-a716-446655440001",
            "message_type": "status",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "farm_id": "FARM_999",
            "barn_id": "BARN_001",
            "zone_id": "ZONE_001",
            "device_id": "DEV_001",
            "payload": {"connection_state": "ONLINE"},
        }
        topic = "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_001/status"
        with self.assertRaises(ValidationError):
            process_incoming_message(topic, message)

    def test_mqtt_topics_require_the_project_prefix(self):
        ids, suffix = parse_topic(
            "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/"
            "zone/ZONE_001/device/DEV_001/telemetry"
        )
        self.assertEqual(ids["device_id"], "DEV_001")
        self.assertEqual(suffix, "telemetry")
        with self.assertRaises(ValidationError):
            parse_topic(
                "farm/FARM_001/barn/BARN_001/zone/ZONE_001/"
                "device/DEV_001/telemetry"
            )

    def test_commands_fail_closed_without_control_token(self):
        response = self.client.post(
            "/api/v1/devices/DEV_001/commands",
            json={"actuator_id": "ACT_FAN_001", "command": "SET_STATE", "parameters": {"state": "ON"}},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"]["code"], "AUTH_NOT_CONFIGURED")

    def test_command_is_audited_as_failed_when_mqtt_is_unavailable(self):
        self.app.config["API_CONTROL_TOKEN"] = "control-test-token"
        response = self.client.post(
            "/api/v1/devices/DEV_001/commands",
            json={
                "command": "OPEN_GATE",
                "parameters": {"actuator": "SERVO_ESP32"},
                "source": "dashboard",
            },
            headers={"Authorization": "Bearer control-test-token"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"]["code"], "MQTT_UNAVAILABLE")
        command = db.session.get(DeviceCommand, response.json["error"]["command_id"])
        self.assertEqual(command.status, "FAILED")

    def test_seed_dev004_registers_inventory_and_stream_url(self):
        self.app.config["SMART_GATE_STREAM_URL"] = "http://127.0.0.1:5001/video_feed"
        result = self.app.test_cli_runner().invoke(args=["seed-dev004"])
        self.assertIsNone(result.exception, result.output)
        repeated = self.app.test_cli_runner().invoke(args=["seed-dev004"])
        self.assertIsNone(repeated.exception, repeated.output)
        device = db.session.get(Device, "DEV_004")
        self.assertEqual((device.farm_id, device.barn_id, device.zone_id), (
            "FARM_001", "BARN_001", "ZONE_001"
        ))
        self.assertEqual(device.stream_url, "http://127.0.0.1:5001/video_feed")
        self.assertEqual(db.session.query(SensorRegistry).count(), 2)
        self.assertEqual(db.session.query(ActuatorRegistry).count(), 2)
        detail = self.client.get("/api/v1/devices/DEV_004")
        self.assertEqual(detail.json["item"]["stream_url"], device.stream_url)

    def test_group4_ai_telemetry_accepts_counts_and_state_strings(self):
        self.app.test_cli_runner().invoke(args=["seed-dev004"])
        readings = [
            {"parameter": name, "value": value, "unit": unit}
            for name, value, unit in (
                ("cow_count", 45, "count"),
                ("farmer_count", 2, "count"),
                ("intruder_count", 1, "count"),
                ("animal_anomaly_count", 0, "count"),
                ("gate_state", "CLOSED", "state"),
                ("alarm_state", "OFF", "state"),
            )
        ]
        for reading in readings:
            reading["sensor_id"] = (
                "SNS_CAM_AI_001"
                if reading["parameter"] in {
                    "cow_count", "farmer_count", "intruder_count", "animal_anomaly_count"
                }
                else "SNS_RFID_001"
            )
            reading["quality"] = "GOOD"
        message = {
            "schema_version": "1.0.0",
            "message_id": "550e8400-e29b-41d4-a716-446655440004",
            "message_type": "telemetry",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "farm_id": "FARM_001",
            "barn_id": "BARN_001",
            "zone_id": "ZONE_001",
            "device_id": "DEV_004",
            "payload": {"readings": readings},
        }
        topic = "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_004/telemetry"
        self.assertTrue(process_incoming_message(topic, message))
        stored = {
            (row.sensor_id, row.parameter): row.value
            for row in db.session.query(Telemetry).all()
        }
        self.assertEqual(stored[("SNS_CAM_AI_001", "cow_count")], 45)
        self.assertEqual(stored[("SNS_RFID_001", "gate_state")], "CLOSED")

        invalid = dict(message)
        invalid["payload"] = {"readings": [{
            "sensor_id": "SNS_CAM_AI_001",
            "parameter": "intruder_count",
            "value": -1,
            "unit": "count",
        }]}
        with self.assertRaises(ValidationError):
            validate_envelope(invalid, "telemetry")

    def test_group4_security_events_store_rfid_metadata(self):
        self.app.test_cli_runner().invoke(args=["seed-dev004"])
        event = {
            "schema_version": "1.0.0",
            "message_id": "550e8400-e29b-41d4-a716-446655440005",
            "message_type": "event",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "farm_id": "FARM_001",
            "barn_id": "BARN_001",
            "zone_id": "ZONE_001",
            "device_id": "DEV_004",
            "payload": {
                "event_id": "550e8400-e29b-41d4-a716-446655440105",
                "event_type": "RFID_ACCESS_DENIED",
                "severity": "WARNING",
                "state": "ACTIVE",
                "metadata": {"uid": "A1B2C3D4"},
            },
        }
        topic = "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_004/event"
        self.assertTrue(process_incoming_message(topic, event))
        stored = db.session.get(FarmEvent, event["payload"]["event_id"])
        self.assertEqual(stored.uid, "A1B2C3D4")
        self.assertEqual(stored.event_metadata, {"uid": "A1B2C3D4"})
        for index, event_type, severity, state in (
            (7, "INTRUDER_DETECTED", "HIGH", "ACTIVE"),
            (8, "ANIMAL_ANOMALY", "WARNING", "ACTIVE"),
            (9, "ALARM_CLEARED", "INFO", "CLEARED"),
        ):
            ai_event = {
                **event,
                "message_id": f"550e8400-e29b-41d4-a716-44665544000{index}",
                "payload": {
                    "event_id": f"550e8400-e29b-41d4-a716-44665544010{index}",
                    "event_type": event_type,
                    "severity": severity,
                    "state": state,
                    "metadata": {"source": "YOLOv8"},
                },
            }
            self.assertTrue(process_incoming_message(topic, ai_event))
        self.assertEqual(db.session.query(FarmEvent).count(), 4)

    def test_group4_commands_publish_to_device_topic_and_accept_ack(self):
        self.app.test_cli_runner().invoke(args=["seed-dev004"])
        device = db.session.get(Device, "DEV_004")
        device.connection_status = "ONLINE"
        db.session.commit()
        service = FakeMQTTService()
        self.app.extensions["mqtt_service"] = service
        self.app.config["API_CONTROL_TOKEN"] = "control-test-token"
        headers = {"Authorization": "Bearer control-test-token"}

        response = self.client.post(
            "/api/v1/devices/DEV_004/commands",
            json={"actuator_id": "ACT_GATE_001", "command": "OPEN_GATE", "parameters": {}},
            headers=headers,
        )
        self.assertEqual(response.status_code, 202)
        topic, published, qos = service.published[0]
        self.assertEqual(
            topic,
            "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_004/command",
        )
        self.assertEqual(published["payload"]["actuator_id"], "ACT_GATE_001")
        self.assertEqual(qos, 1)

        ack = {
            "schema_version": "1.0.0",
            "message_id": "550e8400-e29b-41d4-a716-446655440006",
            "message_type": "command_ack",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "farm_id": "FARM_001",
            "barn_id": "BARN_001",
            "zone_id": "ZONE_001",
            "device_id": "DEV_004",
            "payload": {
                "command_id": response.json["command_id"],
                "actuator_id": "ACT_GATE_001",
                "result": "ACCEPTED",
                "actual_state": "OPEN",
            },
        }
        ack_topic = "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_004/command/ack"
        self.assertTrue(process_incoming_message(ack_topic, ack))
        command = self.client.get(f"/api/v1/commands/{response.json['command_id']}")
        self.assertEqual(command.json["status"], "ACCEPTED")

        for actuator_id, command_name in (
            ("ACT_GATE_001", "CLOSE_GATE"),
            ("ACT_ALARM_001", "ALERT"),
            ("ACT_ALARM_001", "CLEAR"),
        ):
            accepted = self.client.post(
                "/api/v1/devices/DEV_004/commands",
                json={
                    "actuator_id": actuator_id,
                    "command": command_name,
                    "parameters": {},
                },
                headers=headers,
            )
            self.assertEqual(accepted.status_code, 202)

        invalid = self.client.post(
            "/api/v1/devices/DEV_004/commands",
            json={"actuator_id": "ACT_GATE_001", "command": "ALERT", "parameters": {}},
            headers=headers,
        )
        self.assertEqual(invalid.status_code, 400)

    def test_seed_dev002_registers_group2_sensor_inventory(self):
        result = self.app.test_cli_runner().invoke(args=["seed-dev002"])
        self.assertIsNone(result.exception, result.output)
        repeated = self.app.test_cli_runner().invoke(args=["seed-dev002"])
        self.assertIsNone(repeated.exception, repeated.output)
        device = db.session.get(Device, "DEV_002")
        self.assertEqual(device.device_type, "BARN_MONITORING_ML")
        self.assertEqual(db.session.query(SensorRegistry).filter_by(device_id="DEV_002").count(), 4)

    def test_group2_http_sensor_contract_predicts_and_persists_history(self):
        self.app.test_cli_runner().invoke(args=["seed-dev002"])
        predictor = FakeGroup2Model()
        self.app.extensions["group2_model"] = predictor
        response = self.client.post(
            "/api/sensors",
            json={
                "recorded_at": "2026-11-17 10:00:00",
                "temperature_c": 29.6,
                "humidity_rh": 71.3,
            },
            headers={"Origin": "http://localhost:5500"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertIn("Access-Control-Allow-Origin", response.headers)
        self.assertEqual(response.json["status"], "Warning")
        self.assertEqual(predictor.values["mq135_raw"], 0.0)
        self.assertEqual(response.json["defaulted_fields"], ["sound_rms", "mq135_raw"])
        latest = self.client.get("/api/sensors/latest")
        self.assertEqual(latest.json["recorded_at"], "2026-11-17T03:00:00Z")
        self.assertEqual(latest.json["temperature_c"], 29.6)
        history = self.client.get("/api/sensors/history")
        self.assertEqual(history.json["total"], 1)
        self.assertEqual(history.json["data"][0]["status"], "Warning")
        telemetry = self.client.get("/api/v1/telemetry?device_id=DEV_002")
        self.assertEqual(telemetry.json["total"], 4)
        uncertain = [
            item["parameter"]
            for item in telemetry.json["items"]
            if item["quality"] == "UNCERTAIN"
        ]
        self.assertEqual(set(uncertain), {"sound_rms", "mq135_raw"})

    def test_seed_dev003_registers_feeding_and_water_sensors_and_actuators(self):
        result = self.app.test_cli_runner().invoke(args=["seed-dev003"])
        self.assertIsNone(result.exception, result.output)
        repeated = self.app.test_cli_runner().invoke(args=["seed-dev003"])
        self.assertIsNone(repeated.exception, repeated.output)
        device = db.session.get(Device, "DEV_003")
        self.assertEqual(device.device_type, "SMART_FEEDING")
        self.assertEqual(
            db.session.query(SensorRegistry).filter_by(device_id="DEV_003").count(),
            2,
        )
        self.assertEqual(
            db.session.query(ActuatorRegistry).filter_by(device_id="DEV_003").count(),
            2,
        )

    def test_group3_mqtt_telemetry_validates_feed_and_water_levels(self):
        self.app.test_cli_runner().invoke(args=["seed-dev003"])
        readings = [
            ("SNS_ULTRASONIC_003", "distance_cm", 12.5, "cm"),
            ("SNS_ULTRASONIC_003", "feed_level_percent", 58.3, "%"),
            ("SNS_ULTRASONIC_003", "feed_status", "NORMAL", "state"),
            ("SNS_WATER_LEVEL_003", "water_level_percent", 82, "%"),
        ]
        message = {
            "schema_version": "1.0.0",
            "message_id": "550e8400-e29b-41d4-a716-446655440030",
            "message_type": "telemetry",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "farm_id": "FARM_001",
            "barn_id": "BARN_001",
            "zone_id": "ZONE_001",
            "device_id": "DEV_003",
            "payload": {
                "readings": [
                    {
                        "sensor_id": sensor_id,
                        "parameter": parameter,
                        "value": value,
                        "unit": unit,
                        "quality": "GOOD",
                    }
                    for sensor_id, parameter, value, unit in readings
                ]
            },
        }
        topic = "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_003/telemetry"
        self.assertTrue(process_incoming_message(topic, message))
        stored = {
            row.parameter: row.value
            for row in db.session.query(Telemetry).filter_by(device_id="DEV_003").all()
        }
        self.assertEqual(stored["feed_status"], "NORMAL")
        self.assertEqual(stored["water_level_percent"], 82)

        invalid = dict(message)
        invalid["payload"] = {"readings": [{
            "sensor_id": "SNS_WATER_LEVEL_003",
            "parameter": "water_level_percent",
            "value": 101,
            "unit": "%",
        }]}
        with self.assertRaises(ValidationError):
            validate_envelope(invalid, "telemetry")

    def test_group3_pump_and_l298n_commands_are_checked_and_published(self):
        self.app.test_cli_runner().invoke(args=["seed-dev003"])
        device = db.session.get(Device, "DEV_003")
        device.connection_status = "ONLINE"
        db.session.commit()
        service = FakeMQTTService()
        self.app.extensions["mqtt_service"] = service
        self.app.config["API_CONTROL_TOKEN"] = "control-test-token"
        headers = {"Authorization": "Bearer control-test-token"}

        pump = self.client.post(
            "/api/v1/devices/DEV_003/commands",
            json={
                "actuator_id": "ACT_PUMP_003",
                "command": "SET_STATE",
                "parameters": {"state": "ON"},
            },
            headers=headers,
        )
        self.assertEqual(pump.status_code, 202)
        self.assertEqual(
            service.published[0][0],
            "sf-kandang-sapih/farm/FARM_001/barn/BARN_001/zone/ZONE_001/device/DEV_003/command",
        )

        feeder = self.client.post(
            "/api/v1/devices/DEV_003/commands",
            json={
                "actuator_id": "ACT_FEEDER_MOTOR_003",
                "command": "SET_STATE",
                "parameters": {"state": "ON", "speed_percent": 60},
            },
            headers=headers,
        )
        self.assertEqual(feeder.status_code, 202)
        self.assertEqual(
            service.published[1][1]["payload"]["parameters"]["speed_percent"],
            60,
        )

        invalid = self.client.post(
            "/api/v1/devices/DEV_003/commands",
            json={
                "actuator_id": "ACT_FEEDER_MOTOR_003",
                "command": "SET_STATE",
                "parameters": {"state": "ON", "speed_percent": 101},
            },
            headers=headers,
        )
        self.assertEqual(invalid.status_code, 400)


if __name__ == "__main__":
    unittest.main()
