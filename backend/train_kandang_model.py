import json
import os
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler

FEATURES = ("temperature_c", "humidity_rh", "sound_rms", "mq135_raw")
TARGET_CLASSES = ["Kritis", "Normal", "Warning"]


def compute_thi(temp: float, rh: float) -> float:
    """Calculate Temperature-Humidity Index (THI) for livestock."""
    return 0.8 * temp + (rh / 100.0) * (temp - 14.4) + 46.4


def generate_synthetic_data(n_samples_per_class: int = 768, random_state: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(random_state)
    records = []

    # 1. WARNING (Perlu Penanganan)
    # Suhu 30.5 - 33.0°C, RH 70 - 85%, Sound RMS 80k - 250k, MQ-135 700 - 1200
    for _ in range(n_samples_per_class):
        temp = float(np.clip(rng.normal(31.5, 0.8), 30.2, 33.0))
        rh = float(np.clip(rng.normal(76.0, 4.0), 68.0, 86.0))
        sound = float(np.clip(rng.exponential(90000) + 40000, 20000, 260000))
        mq = float(np.clip(rng.normal(850, 140), 650, 1250))
        records.append({
            "temperature_c": round(temp, 1),
            "humidity_rh": round(rh, 1),
            "sound_rms": round(sound),
            "mq135_raw": round(mq),
            "label": "Warning",
        })

    # 2. KRITIS
    # Suhu > 33.0°C (Heat stress tinggi), RH > 78%, Sound RMS > 250k, MQ-135 > 1200
    for _ in range(n_samples_per_class):
        temp = float(np.clip(rng.normal(34.5, 1.2), 33.1, 38.5))
        rh = float(np.clip(rng.normal(82.0, 5.0), 72.0, 95.0))
        sound = float(np.clip(rng.exponential(200000) + 150000, 80000, 900000))
        mq = float(np.clip(rng.normal(1450, 250), 1150, 2500))
        records.append({
            "temperature_c": round(temp, 1),
            "humidity_rh": round(rh, 1),
            "sound_rms": round(sound),
            "mq135_raw": round(mq),
            "label": "Kritis",
        })

    return pd.DataFrame(records)


def train_and_export(csv_path: str | Path, output_dirs: list[str | Path]):
    csv_file = Path(csv_path)
    if not csv_file.is_file():
        raise FileNotFoundError(f"Dataset not found at {csv_file}")

    print(f"Loading base dataset from {csv_file}...")
    df_raw = pd.read_csv(csv_file)
    print(f"Found {len(df_raw)} records in raw dataset.")

    # Base dataset from classroom recorded as Normal
    df_normal = pd.DataFrame({
        "temperature_c": df_raw["temperature_c"].astype(float),
        "humidity_rh": df_raw["humidity_rh"].astype(float),
        "sound_rms": df_raw["sound_rms"].astype(float),
        "mq135_raw": df_raw["mq135_raw"].astype(float),
        "label": "Normal",
    })

    # Generate synthetic Warning & Kritis samples
    print(f"Generating synthetic Warning and Kritis samples based on THI & farm limits...")
    df_synthetic = generate_synthetic_data(n_samples_per_class=len(df_normal), random_state=42)

    # Combine
    df_full = pd.concat([df_normal, df_synthetic], ignore_index=True)
    print("Class distribution:")
    print(df_full["label"].value_counts())

    X = df_full[list(FEATURES)]
    y = df_full["label"]

    # Encode labels
    label_encoder = LabelEncoder()
    label_encoder.fit(TARGET_CLASSES)
    y_encoded = label_encoder.transform(y)

    # Scale features
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Train-test split
    X_train, X_test, y_train, y_test = train_test_split(
        X_scaled, y_encoded, test_size=0.2, random_state=42, stratify=y_encoded
    )

    print("Training RandomForestClassifier...")
    model = RandomForestClassifier(
        n_estimators=100,
        max_depth=12,
        min_samples_split=4,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)

    # Evaluate
    y_pred = model.predict(X_test)
    acc = float(accuracy_score(y_test, y_pred))
    print(f"\nAccuracy on test set: {acc * 100:.2f}%")
    print("\nClassification Report:")
    print(classification_report(y_test, y_pred, target_names=label_encoder.classes_))

    # Prepare metadata
    metadata = {
        "model": "RandomForestClassifier",
        "features": list(FEATURES),
        "classes": list(label_encoder.classes_),
        "accuracy": round(acc, 4),
        "description": "Moo-Nitoring Kandang Sapi environmental condition model trained on baseline and THI augmented data.",
    }

    # Save to all target output directories
    for out_dir in output_dirs:
        dest = Path(out_dir)
        dest.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, dest / "rf_kandang.pkl")
        joblib.dump(scaler, dest / "scaler_kandang.pkl")
        joblib.dump(label_encoder, dest / "label_encoder.pkl")
        with (dest / "model_metadata.json").open("w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        print(f"Model artifacts exported successfully to {dest.resolve()}")


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parent.parent
    csv_input = repo_root / "sensor_data.csv"
    if not csv_input.exists():
        csv_input = Path("sensor_data.csv")

    output_directories = [
        repo_root / "backend" / "model",
        repo_root.parent / "backend kel 2" / "model",
    ]
    train_and_export(csv_input, output_directories)
