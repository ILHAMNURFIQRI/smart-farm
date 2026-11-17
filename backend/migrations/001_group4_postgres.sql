-- Apply once to databases created with the earlier backend schema.
ALTER TABLE device
    ADD COLUMN stream_url VARCHAR(2048);

ALTER TABLE telemetry
    ALTER COLUMN value TYPE JSON USING to_json(value);
ALTER TABLE telemetry
    DROP CONSTRAINT uq_telemetry_message_sensor;
ALTER TABLE telemetry
    ADD CONSTRAINT uq_telemetry_message_sensor_parameter
    UNIQUE (message_id, sensor_id, parameter);

ALTER TABLE farm_event
    ADD COLUMN uid VARCHAR(64),
    ADD COLUMN metadata JSON NOT NULL DEFAULT '{}'::json;

CREATE TABLE sensor_registry (
    sensor_id VARCHAR(64) PRIMARY KEY,
    device_id VARCHAR(64) NOT NULL REFERENCES device(device_id),
    sensor_type VARCHAR(64) NOT NULL,
    parameters JSON NOT NULL DEFAULT '[]'::json,
    unit VARCHAR(32),
    metadata JSON NOT NULL DEFAULT '{}'::json
);
CREATE INDEX ix_sensor_registry_device_id ON sensor_registry(device_id);

CREATE TABLE actuator_registry (
    actuator_id VARCHAR(64) PRIMARY KEY,
    device_id VARCHAR(64) NOT NULL REFERENCES device(device_id),
    actuator_type VARCHAR(64) NOT NULL,
    supported_commands JSON NOT NULL DEFAULT '[]'::json,
    metadata JSON NOT NULL DEFAULT '{}'::json
);
CREATE INDEX ix_actuator_registry_device_id ON actuator_registry(device_id);
