"""
Moo-nitoring Mikroklimat Kandang (K2) - Modular Backend Service
Handles sensor telemetry ingestion, Random Forest classification, comfort score calculation,
actuator automation, early warning trends, and sanitation logs for DEV_002.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request

from group2_model import Group2Model, Group2ModelUnavailable
from models import (
    BarnRecoveryLog,
    Device,
    FarmEvent,
    Group2Prediction,
    SanitationLog,
    SensorRegistry,
    Telemetry,
    as_utc,
    db,
    utc_now,
)
from schemas import (
    ValidationError,
    isoformat_utc,
    parse_timestamp,
)

moo_nitoring_bp = Blueprint("moo_nitoring", __name__)

GROUP2_FEATURES = ("temperature_c", "humidity_rh", "sound_rms", "mq135_raw")


def get_group2_model():
    """Retrieve or lazily initialize the trained Random Forest model for DEV_002."""
    model = current_app.extensions.get("group2_model")
    if model is None:
        model_dir = current_app.config.get("GROUP2_MODEL_DIR")
        if not model_dir:
            local_model_dir = Path(current_app.root_path) / "model"
            if (local_model_dir / "rf_kandang.pkl").is_file():
                model_dir = local_model_dir
            else:
                model_dir = Path(current_app.root_path).parent / "backend kel 2" / "model"
        model = Group2Model(model_dir)
        current_app.extensions["group2_model"] = model
    return model


def parse_group2_timestamp(value):
    """Parse timestamps sent from ESP32 or external clients into UTC datetime."""
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
            raise ValidationError("recorded_at must be an ISO 8601 timestamp") from exc
        return local_time.replace(tzinfo=timezone(timedelta(hours=7))).astimezone(
            timezone.utc
        )


def calculate_thi(temp: float, rh: float) -> float:
    """Calculate Temperature-Humidity Index (THI) for livestock."""
    return round(0.8 * temp + (rh / 100.0) * (temp - 14.4) + 46.4, 1)


def calculate_comfort_metrics(
    temperature_c: float,
    humidity_rh: float,
    sound_rms: float,
    mq135_raw: float,
) -> dict:
    """
    Calculate livestock comfort score (0-100) and THI index tuned for tropical dairy barns.
    """
    thi = calculate_thi(temperature_c, humidity_rh)
    score = 100.0

    # THI Curve tuned for tropical dairy barn conditions
    if thi > 86.0:
        score -= min(45.0, (thi - 86.0) * 8.0 + 20.0)
    elif thi > 82.0:
        score -= (thi - 82.0) * 3.5 + 8.0
    elif thi > 78.0:
        score -= (thi - 78.0) * 2.0
    elif thi < 68.0:
        score -= (68.0 - thi) * 1.5

    # Calibrated for normalized float RMS sound from INMP441
    if sound_rms > 0.20:
        score -= min(25.0, (sound_rms - 0.20) * 50.0 + 10.0)
    elif sound_rms > 0.05:
        score -= (sound_rms - 0.05) * 40.0

    # MQ-135 Gas penalty
    if mq135_raw > 600:
        score -= min(25.0, ((mq135_raw - 600) / 400.0) * 20.0 + 5.0)
    elif mq135_raw > 350:
        score -= ((mq135_raw - 350) / 250.0) * 5.0

    score = round(max(0.0, min(100.0, score)), 1)

    if score >= 75.0:
        kategori = "Normal"
        interpretasi = "Kondisi lingkungan relatif sesuai rentang target."
    elif score >= 50.0:
        kategori = "Perlu Penanganan"
        interpretasi = "Terdapat parameter yang mulai menyimpang dan perlu diperhatikan."
    else:
        kategori = "Kritis"
        interpretasi = "Kondisi lingkungan kritis, segera lakukan penanganan kandang."

    return {
        "comfort_score": score,
        "thi": thi,
        "kategori": kategori,
        "interpretasi": interpretasi,
    }


def group2_prediction_json(prediction: Group2Prediction) -> dict:
    """Format a Group2Prediction record into API response format."""
    comfort = calculate_comfort_metrics(
        float(prediction.sensor_values.get("temperature_c", 0.0)),
        float(prediction.sensor_values.get("humidity_rh", 0.0)),
        float(prediction.sensor_values.get("sound_rms", 0.0)),
        float(prediction.sensor_values.get("mq135_raw", 0.0)),
    )
    wib_time = as_utc(prediction.received_at).astimezone(timezone(timedelta(hours=7))).strftime("%H:%M:%S")
    return {
        "recorded_at": isoformat_utc(prediction.timestamp),
        "received_at": isoformat_utc(prediction.received_at),
        "time_wib": wib_time,
        **prediction.sensor_values,
        "status": prediction.prediction_status,
        "confidence": prediction.confidence,
        "probabilitas": prediction.probabilities,
        "defaulted_fields": prediction.defaulted_fields,
        "comfort": comfort,
    }


def get_kandang_early_warning(device_id="DEV_002") -> dict:
    """Calculate 15-minute temperature trend and early warning prediction."""
    rows = db.session.scalars(
        db.select(Group2Prediction)
        .where(Group2Prediction.device_id == device_id)
        .order_by(Group2Prediction.id.desc())
        .limit(10)
    ).all()
    if not rows:
        return {
            "trend": "UNKNOWN",
            "slope_per_min": 0.0,
            "current_temp": None,
            "estimated_temp_15m": None,
            "status": "NORMAL",
            "message": "Belum ada data untuk kalkulasi peringatan dini.",
            "recommendation": "Tunggu data telemetri terkumpul.",
        }
    rows.reverse()
    last_temp = float(rows[-1].sensor_values.get("temperature_c", 0.0))
    if len(rows) < 3:
        return {
            "trend": "STABIL",
            "slope_per_min": 0.0,
            "current_temp": last_temp,
            "estimated_temp_15m": last_temp,
            "status": "NORMAL",
            "message": "Parameter stabil dalam batas pemantauan (data awal sedang dihimpun).",
            "recommendation": "Lakukan pemantauan rutin berkala.",
        }

    t_first = as_utc(rows[0].timestamp)
    t_last = as_utc(rows[-1].timestamp)
    dt_seconds = (t_last - t_first).total_seconds()
    dt_minutes = max(dt_seconds / 60.0, 0.5)

    t_first_val = float(rows[0].sensor_values.get("temperature_c", last_temp))
    slope_per_min = (last_temp - t_first_val) / dt_minutes

    if slope_per_min >= 0.05:
        trend = "MENINGKAT"
        est_15m = round(last_temp + slope_per_min * 15.0, 1)
        if est_15m >= 32.0 or last_temp >= 31.0:
            status = "WARNING"
            message = f"Peringatan dini: potensi suhu tinggi meningkat (+{round(slope_per_min * 10, 2)}°C/10min). Estimasi 15 menit berikutnya: {est_15m}°C."
            recommendation = "Periksa sirkulasi udara dan kesiapan kipas."
        else:
            status = "ATTENTION"
            message = f"Suhu menunjukkan tren kenaikan moderat. Estimasi 15 menit berikutnya: {est_15m}°C."
            recommendation = "Pantau ventilasi kandang."
    elif slope_per_min <= -0.05:
        trend = "MENURUN"
        est_15m = round(last_temp + slope_per_min * 15.0, 1)
        status = "NORMAL"
        message = "Suhu kandang bergerak turun menuju kondisi stabil."
        recommendation = "Pertahankan pendinginan/ventilasi saat ini."
    else:
        trend = "STABIL"
        est_15m = round(last_temp, 1)
        status = "NORMAL"
        message = "Parameter suhu dan kelembapan stabil dalam batas pemantauan ideal."
        recommendation = "Lakukan pemantauan rutin berkala."

    return {
        "trend": trend,
        "slope_per_min": round(slope_per_min, 3),
        "current_temp": last_temp,
        "estimated_temp_15m": est_15m,
        "status": status,
        "message": message,
        "recommendation": recommendation,
    }


def get_active_recovery_session(device_id="DEV_002"):
    return db.session.scalars(
        db.select(BarnRecoveryLog)
        .where(
            BarnRecoveryLog.device_id == device_id,
            BarnRecoveryLog.status == "IN_PROGRESS",
        )
        .order_by(BarnRecoveryLog.id.desc())
        .limit(1)
    ).first()


def recovery_log_json(session):
    if session is None:
        return None
    return {
        "id": session.id,
        "device_id": session.device_id,
        "actuator_id": session.actuator_id,
        "start_time": isoformat_utc(session.start_time),
        "end_time": isoformat_utc(session.end_time) if session.end_time else None,
        "initial_temperature": session.initial_temperature,
        "target_temperature": session.target_temperature,
        "final_temperature": session.final_temperature,
        "evaluation_minutes": session.evaluation_minutes,
        "duration_minutes": session.duration_minutes,
        "status": session.status,
        "recommendation": session.recommendation,
    }


def update_recovery_sessions(device_id="DEV_002", current_temp=None, recorded_at=None):
    session = get_active_recovery_session(device_id)
    if session is None or current_temp is None:
        return session
    now = recorded_at or utc_now()
    elapsed_minutes = (as_utc(now) - as_utc(session.start_time)).total_seconds() / 60.0

    if current_temp <= session.target_temperature:
        session.status = "RECOVERED"
        session.end_time = now
        session.final_temperature = current_temp
        session.duration_minutes = round(max(0.1, elapsed_minutes), 1)
        session.recommendation = f"Suhu lingkungan kandang berhasil kembali ke rentang target ({session.target_temperature}°C)."
    elif elapsed_minutes >= session.evaluation_minutes:
        session.status = "TIMEOUT_EXCEEDED"
        session.end_time = now
        session.final_temperature = current_temp
        session.duration_minutes = round(elapsed_minutes, 1)
        session.recommendation = "Periksa sirkulasi udara, kipas, penempatan sensor, dan kondisi kandang."
    return session


# ─── BLUEPRINT ROUTES ────────────────────────────────────────────────────────


@moo_nitoring_bp.post("/api/sensors")
def receive_group2_sensor():
    body = request.get_json(silent=False)
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")

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
        current_app.logger.error("Group 2 model unavailable: %s", exc)
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

    # Update comfort metrics and active recovery sessions
    update_recovery_sessions(
        device_id=device.device_id,
        current_temp=values["temperature_c"],
        recorded_at=recorded_at,
    )
    comfort = calculate_comfort_metrics(
        values["temperature_c"],
        values["humidity_rh"],
        values["sound_rms"],
        values["mq135_raw"],
    )

    # Otomasi Aktuator Kipas & LED Kandang
    cfg = (device.config or {}).get("automation", {
        "fan_auto": True,
        "fan_temp_on": 30.5,
        "fan_temp_off": 28.5,
        "led_auto": True,
        "led_start_time": "18:00",
        "led_end_time": "06:00",
    })

    cur_temp = values["temperature_c"]
    fan_target_state = None
    if cfg.get("fan_auto", True):
        f_on = float(cfg.get("fan_temp_on", 30.5))
        f_off = float(cfg.get("fan_temp_off", 28.5))
        if cur_temp >= f_on:
            fan_target_state = "ON"
        elif cur_temp <= f_off:
            fan_target_state = "OFF"

    local_now = (recorded_at or utc_now()).astimezone(timezone(timedelta(hours=7)))
    led_target_state = None
    if cfg.get("led_auto", True):
        s_time = cfg.get("led_start_time", "18:00")
        e_time = cfg.get("led_end_time", "06:00")
        cur_hhmm = local_now.strftime("%H:%M")
        if s_time > e_time:
            led_target_state = "ON" if (cur_hhmm >= s_time or cur_hhmm < e_time) else "OFF"
        else:
            led_target_state = "ON" if (s_time <= cur_hhmm < e_time) else "OFF"

    automation_status = {
        "fan_target_state": fan_target_state,
        "led_target_state": led_target_state,
        "config": cfg,
    }

    print(
        f"📥 [DEV_002 TELEMETRY] Suhu: {values['temperature_c']}°C | "
        f"RH: {values['humidity_rh']}% | "
        f"THI: {comfort['thi']} ({comfort['kategori']}) | "
        f"Gas MQ: {values['mq135_raw']} | "
        f"Sound RMS: {values['sound_rms']:.3f} | "
        f"Status ML: {prediction_result['status']} ({prediction_result['confidence']}%)",
        flush=True,
    )

    db.session.commit()
    return jsonify({
        "message": "Data diterima",
        "device_id": device.device_id,
        "recorded_at": isoformat_utc(recorded_at),
        **prediction_result,
        "comfort": comfort,
        "automation": automation_status,
        "defaulted_fields": defaulted_fields,
    }), 201


@moo_nitoring_bp.get("/api/sensors/latest")
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


@moo_nitoring_bp.get("/api/sensors/history")
def get_group2_history():
    limit_param = request.args.get("limit", "50")
    period = request.args.get("period", "day")
    try:
        limit = min(200, max(5, int(limit_param)))
    except ValueError:
        limit = 50

    if period in ["week", "month", "year"]:
        predictions = db.session.scalars(
            db.select(Group2Prediction)
            .where(Group2Prediction.device_id == "DEV_002")
            .order_by(Group2Prediction.timestamp.asc())
        ).all()

        if not predictions:
            return jsonify({"total": 0, "data": [], "period": period, "is_aggregated": True})

        groups = {}
        for p in predictions:
            dt = as_utc(p.timestamp).astimezone(timezone(timedelta(hours=7)))
            if period == "year":
                key = dt.strftime("%Y-%m")
                label = dt.strftime("%b %Y")
            else:
                key = dt.strftime("%Y-%m-%d")
                label = dt.strftime("%d %b")

            if key not in groups:
                groups[key] = {
                    "label": label,
                    "temps": [],
                    "hums": [],
                    "this": [],
                    "mqs": [],
                    "sounds": [],
                    "scores": [],
                }
            vals = p.sensor_values or {}
            t = float(vals.get("temperature_c", 0.0))
            h = float(vals.get("humidity_rh", 0.0))
            mq = float(vals.get("mq135_raw", 0.0))
            s = float(vals.get("sound_rms", 0.0))
            c = calculate_comfort_metrics(t, h, s, mq)
            groups[key]["temps"].append(t)
            groups[key]["hums"].append(h)
            groups[key]["this"].append(c["thi"])
            groups[key]["mqs"].append(mq)
            groups[key]["sounds"].append(s)
            groups[key]["scores"].append(c["comfort_score"])

        aggregated = []
        for k in sorted(groups.keys()):
            g = groups[k]
            n = len(g["temps"]) or 1
            aggregated.append({
                "recorded_at": f"{k}T00:00:00Z",
                "time_label": g["label"],
                "temperature_c": round(sum(g["temps"]) / n, 1),
                "humidity_rh": round(sum(g["hums"]) / n, 1),
                "thi": round(sum(g["this"]) / n, 1),
                "mq135_raw": round(sum(g["mqs"]) / n, 0),
                "sound_rms": round(sum(g["sounds"]) / n, 4),
                "comfort": {
                    "comfort_score": round(sum(g["scores"]) / n, 1),
                    "thi": round(sum(g["this"]) / n, 1),
                },
                "samples_count": n,
            })
        return jsonify({
            "total": len(aggregated),
            "data": aggregated,
            "period": period,
            "is_aggregated": True,
        })

    # Default: period == "day"
    rows = db.session.scalars(
        db.select(Group2Prediction)
        .where(Group2Prediction.device_id == "DEV_002")
        .order_by(Group2Prediction.id.desc())
        .limit(limit)
    ).all()
    rows.reverse()
    return jsonify({
        "total": len(rows),
        "data": [group2_prediction_json(row) for row in rows],
        "period": "day",
        "is_aggregated": False,
    })


@moo_nitoring_bp.get("/api/v1/kandang/status")
def get_kandang_status():
    latest = db.session.scalars(
        db.select(Group2Prediction)
        .where(Group2Prediction.device_id == "DEV_002")
        .order_by(Group2Prediction.id.desc())
        .limit(1)
    ).first()
    device = db.session.get(Device, "DEV_002")
    active_recovery = get_active_recovery_session("DEV_002")
    early_warning = get_kandang_early_warning("DEV_002")

    automation_config = (device.config or {}).get("automation", {
        "fan_auto": True,
        "fan_temp_on": 30.5,
        "fan_temp_off": 28.5,
        "led_auto": True,
        "led_start_time": "18:00",
        "led_end_time": "06:00",
    }) if device else {}

    device_dict = {
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
    } if device else None

    if latest is None:
        return jsonify({
            "device": device_dict,
            "latest_prediction": None,
            "comfort": None,
            "early_warning": early_warning,
            "recovery_session": recovery_log_json(active_recovery),
            "automation": automation_config,
        }), 200

    pred_json = group2_prediction_json(latest)
    return jsonify({
        "device": device_dict,
        "latest_prediction": pred_json,
        "comfort": pred_json.get("comfort"),
        "early_warning": early_warning,
        "recovery_session": recovery_log_json(active_recovery),
        "automation": automation_config,
    }), 200


@moo_nitoring_bp.get("/api/v1/kandang/automation")
def get_kandang_automation():
    device = db.session.get(Device, "DEV_002")
    config = (device.config or {}).get("automation", {
        "fan_auto": True,
        "fan_temp_on": 30.5,
        "fan_temp_off": 28.5,
        "led_auto": True,
        "led_start_time": "18:00",
        "led_end_time": "06:00",
    }) if device else {}
    return jsonify({"automation": config})


@moo_nitoring_bp.post("/api/v1/kandang/automation")
def update_kandang_automation():
    body = request.get_json(silent=True) or {}
    device = db.session.get(Device, "DEV_002")
    if not device:
        raise ValidationError("Device DEV_002 tidak ditemukan")

    fan_temp_on = body.get("fan_temp_on", 30.5)
    fan_temp_off = body.get("fan_temp_off", 28.5)
    led_start_time = body.get("led_start_time", "18:00")
    led_end_time = body.get("led_end_time", "06:00")
    fan_auto = bool(body.get("fan_auto", True))
    led_auto = bool(body.get("led_auto", True))

    # Reasonable bounds validation
    try:
        fan_temp_on = round(float(fan_temp_on), 1)
        fan_temp_off = round(float(fan_temp_off), 1)
    except (TypeError, ValueError):
        raise ValidationError("Suhu ambang batas harus berupa angka desimal valid.")

    if not (28.0 <= fan_temp_on <= 36.0):
        raise ValidationError("Suhu nyala kipas otomatis harus dalam rentang wajar kandang sapi tropis (28.0°C - 36.0°C).")
    if not (26.0 <= fan_temp_off <= 34.0):
        raise ValidationError("Suhu mati kipas otomatis harus dalam rentang wajar (26.0°C - 34.0°C).")
    if fan_temp_off >= fan_temp_on:
        raise ValidationError("Suhu mati kipas harus lebih rendah dari suhu nyala (histeresis minimal 0.5°C).")

    time_pattern = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
    if not time_pattern.match(str(led_start_time)) or not time_pattern.match(str(led_end_time)):
        raise ValidationError("Format waktu lampu sirkadian harus HH:MM (contoh: 18:00 dan 06:00).")

    cfg = dict(device.config or {})
    cfg["automation"] = {
        "fan_auto": fan_auto,
        "fan_temp_on": fan_temp_on,
        "fan_temp_off": fan_temp_off,
        "led_auto": led_auto,
        "led_start_time": led_start_time,
        "led_end_time": led_end_time,
    }
    device.config = cfg
    db.session.commit()
    return jsonify({
        "message": "Pengaturan otomatisasi kandang berhasil disimpan",
        "automation": cfg["automation"],
    }), 200


@moo_nitoring_bp.get("/api/v1/kandang/sanitation")
def get_kandang_sanitation():
    logs = db.session.scalars(
        db.select(SanitationLog)
        .where(SanitationLog.device_id == "DEV_002")
        .order_by(SanitationLog.id.desc())
        .limit(30)
    ).all()
    return jsonify({
        "total": len(logs),
        "items": [
            {
                "id": l.id,
                "scheduled_at": isoformat_utc(l.scheduled_at),
                "completed_at": isoformat_utc(l.completed_at) if l.completed_at else None,
                "cleaning_type": l.cleaning_type,
                "operator_name": l.operator_name,
                "status": l.status,
                "notes": l.notes,
                "created_at": isoformat_utc(l.created_at),
            }
            for l in logs
        ],
    }), 200


@moo_nitoring_bp.post("/api/v1/kandang/sanitation")
def create_kandang_sanitation():
    body = request.get_json(silent=True) or {}
    cleaning_type = (body.get("cleaning_type") or "Sanitasi & Semprot Kandang").strip()
    operator_name = (body.get("operator_name") or "Tim Peternak").strip()
    status = (body.get("status") or "SELESAI").strip().upper()
    notes = (body.get("notes") or "").strip()
    scheduled_at_str = body.get("scheduled_at")

    scheduled_at = parse_timestamp(scheduled_at_str, "scheduled_at") if scheduled_at_str else utc_now()
    completed_at = utc_now() if status == "SELESAI" else None

    log = SanitationLog(
        device_id="DEV_002",
        scheduled_at=scheduled_at,
        completed_at=completed_at,
        cleaning_type=cleaning_type,
        operator_name=operator_name,
        status=status,
        notes=notes,
    )
    db.session.add(log)
    db.session.commit()
    return jsonify({
        "message": "Catatan sanitasi berhasil disimpan",
        "log": {
            "id": log.id,
            "scheduled_at": isoformat_utc(log.scheduled_at),
            "completed_at": isoformat_utc(log.completed_at) if log.completed_at else None,
            "cleaning_type": log.cleaning_type,
            "operator_name": log.operator_name,
            "status": log.status,
            "notes": log.notes,
        },
    }), 201


@moo_nitoring_bp.get("/api/v1/kandang/early-warning")
def get_kandang_early_warning_endpoint():
    return jsonify(get_kandang_early_warning("DEV_002")), 200


@moo_nitoring_bp.get("/api/v1/kandang/recovery-history")
def get_kandang_recovery_history():
    rows = db.session.scalars(
        db.select(BarnRecoveryLog)
        .where(BarnRecoveryLog.device_id == "DEV_002")
        .order_by(BarnRecoveryLog.id.desc())
        .limit(20)
    ).all()
    recovered_count = sum(1 for r in rows if r.status == "RECOVERED")
    exceeded_count = sum(1 for r in rows if r.status == "TIMEOUT_EXCEEDED")
    durations = [r.duration_minutes for r in rows if r.status == "RECOVERED" and r.duration_minutes is not None]
    avg_recovery = round(sum(durations) / len(durations), 1) if durations else None

    return jsonify({
        "total": len(rows),
        "summary": {
            "recovered_count": recovered_count,
            "timeout_exceeded_count": exceeded_count,
            "average_recovery_minutes": avg_recovery,
        },
        "items": [recovery_log_json(r) for r in rows],
    }), 200


@moo_nitoring_bp.get("/health")
def group2_health():
    try:
        predictor = get_group2_model()
    except Group2ModelUnavailable as exc:
        current_app.logger.error("Group 2 model unavailable: %s", exc)
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
    }), 200


@moo_nitoring_bp.get("/model/info")
def group2_model_info():
    try:
        predictor = get_group2_model()
    except Group2ModelUnavailable as exc:
        current_app.logger.error("Group 2 model unavailable: %s", exc)
        return jsonify({
            "error": {
                "code": "MODEL_UNAVAILABLE",
                "message": "Group 2 model is not configured or could not be loaded",
            }
        }), 503
    return jsonify(predictor.metadata), 200
