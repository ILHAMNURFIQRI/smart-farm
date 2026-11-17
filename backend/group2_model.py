import json
import pickle
from pathlib import Path
from typing import Any

import joblib
import pandas as pd


FEATURES = ("temperature_c", "humidity_rh", "sound_rms", "mq135_raw")
MODEL_FILES = ("rf_kandang.pkl", "scaler_kandang.pkl", "label_encoder.pkl")


class Group2ModelUnavailable(RuntimeError):
    pass


class Group2Model:
    def __init__(self, model_dir: str | Path):
        directory = Path(model_dir)
        missing = [name for name in (*MODEL_FILES, "model_metadata.json") if not (directory / name).is_file()]
        if missing:
            raise Group2ModelUnavailable(
                f"Group 2 model files are missing in {directory}: {', '.join(missing)}"
            )
        try:
            with (directory / "model_metadata.json").open(encoding="utf-8") as file:
                self.metadata = json.load(file)
            if tuple(self.metadata.get("features", ())) != FEATURES:
                raise Group2ModelUnavailable("Group 2 model feature order does not match the sensor API")
            self.model = joblib.load(directory / "rf_kandang.pkl")
            self.scaler = joblib.load(directory / "scaler_kandang.pkl")
            self.label_encoder = joblib.load(directory / "label_encoder.pkl")
        except Group2ModelUnavailable:
            raise
        except (
            OSError,
            EOFError,
            pickle.UnpicklingError,
            ImportError,
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
        ) as exc:
            raise Group2ModelUnavailable(f"Could not load Group 2 model from {directory}") from exc

    def predict(self, values: dict[str, float]) -> dict[str, Any]:
        features = pd.DataFrame(
            [[values[name] for name in FEATURES]],
            columns=FEATURES,
            dtype=float,
        )
        scaled = self.scaler.transform(features)
        encoded = self.model.predict(scaled)[0]
        probabilities = self.model.predict_proba(scaled)[0]
        status = str(self.label_encoder.inverse_transform([encoded])[0])
        return {
            "status": status,
            "confidence": round(float(probabilities.max()) * 100, 2),
            "probabilitas": {
                str(label): round(float(probability) * 100, 2)
                for label, probability in zip(self.label_encoder.classes_, probabilities)
            },
        }
