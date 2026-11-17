-- Apply once to databases created before Group 2 model integration.
CREATE TABLE group2_prediction (
    id SERIAL PRIMARY KEY,
    device_id VARCHAR(64) NOT NULL REFERENCES device(device_id),
    timestamp TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    sensor_values JSON NOT NULL,
    prediction_status VARCHAR(16) NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    probabilities JSON NOT NULL,
    defaulted_fields JSON NOT NULL DEFAULT '[]'::json
);
CREATE INDEX ix_group2_prediction_device_id ON group2_prediction(device_id);
CREATE INDEX ix_group2_prediction_timestamp ON group2_prediction(timestamp);
CREATE INDEX ix_group2_prediction_prediction_status ON group2_prediction(prediction_status);
