"""Mid-only logistic edge model for 15-minute KXBTC15M markets.

Loads ``model.json`` and scores P(YES settles) from
``yes_mid``, ``minutes_left``, and ``ret_1m`` (missing ret imputed to 0).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "model.json"

from filters import MAX_ENTRY_PREMIUM

# Paper / scan gates (BTC). ETH is not scored with this model.
THR = 0.05
ASK_LO = 0.40
ASK_HI = MAX_ENTRY_PREMIUM  # skip ask/premium > 0.65 (mm loser autopsy)
MAX_SPREAD = 0.02
MIN_MINUTES_LEFT = 2.0

_MODEL: dict[str, Any] | None = None


def load_model(path: Path | str | None = None) -> dict[str, Any]:
    """Load and cache model.json. Safe to call repeatedly."""
    global _MODEL
    p = Path(path) if path is not None else MODEL_PATH
    if _MODEL is not None and path is None:
        return _MODEL
    with open(p, encoding="utf-8") as f:
        model = json.load(f)
    names = model.get("feature_names") or ["yes_mid", "minutes_left", "ret_1m"]
    coef = [float(x) for x in model["coef"]]
    if len(coef) != len(names):
        raise ValueError(f"coef length {len(coef)} != feature_names {len(names)}")
    model = dict(model)
    model["feature_names"] = list(names)
    model["coef"] = coef
    model["intercept"] = float(model["intercept"])
    if path is None:
        _MODEL = model
    return model


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _get_num(features: Mapping[str, Any], key: str, default: float | None = None) -> float | None:
    if key not in features or features[key] is None:
        return default
    try:
        return float(features[key])
    except (TypeError, ValueError):
        return default


def feature_vector(features: Mapping[str, Any], model: Mapping[str, Any] | None = None) -> list[float]:
    """Build the design vector; impute missing ``ret_1m`` to 0."""
    m = model or load_model()
    names = m["feature_names"]
    vec: list[float] = []
    for name in names:
        default = 0.0 if name == "ret_1m" else None
        val = _get_num(features, name, default)
        if val is None:
            raise KeyError(f"missing required feature {name!r}")
        vec.append(val)
    return vec


def model_p(features: dict) -> float:
    """P(YES) from logistic sigmoid on coef · x + intercept.

    ``features`` must include ``yes_mid`` and ``minutes_left``.
    Missing ``ret_1m`` is imputed to 0.
    """
    model = load_model()
    x = feature_vector(features, model)
    z = model["intercept"]
    for c, v in zip(model["coef"], x):
        z += c * v
    p = _sigmoid(z)
    if p < 0.0:
        return 0.0
    if p > 1.0:
        return 1.0
    return p


def edge_yes(p: float, yes_ask: float) -> float:
    """Expected edge of buying YES at the ask and holding to settlement."""
    return float(p) - float(yes_ask)


def gate_flags(
    *,
    yes_ask: float,
    spread: float,
    minutes_left: float,
    edge: float,
    thr: float = THR,
) -> dict[str, bool]:
    return {
        "ask_band": ASK_LO <= yes_ask <= ASK_HI,
        "spread": spread <= MAX_SPREAD and spread >= 0.0,
        "time": minutes_left >= MIN_MINUTES_LEFT,
        "thr": edge >= thr,
    }


def gates_pass(flags: Mapping[str, bool]) -> bool:
    return all(bool(v) for v in flags.values())
