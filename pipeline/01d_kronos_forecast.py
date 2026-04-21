"""
Step 01D: Kronos AI Price Forecast
Runs Kronos-small (24.7M params) on each ticker's daily OHLCV history and
produces a 5-day directional forecast used as kronos_mult in step 06 scoring.

Setup (one-time):
    cd ~/Desktop/News_Spread_Engine
    git clone https://github.com/shiyu-coder/Kronos vendor/kronos
    pip install torch transformers huggingface_hub pandas

On M1 Macs, PyTorch will automatically use MPS (Apple Silicon) acceleration.
Model weights (~200 MB) download from HuggingFace on first run and cache locally.

If Kronos is not installed this step writes neutral signals (×1.00) so the
rest of the pipeline continues normally.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KRONOS_DIR = os.path.join(_BASE_DIR, "vendor", "kronos")
sys.path.insert(0, KRONOS_DIR)

try:
    import torch
    from model import Kronos, KronosTokenizer, KronosPredictor
    _KRONOS_AVAILABLE = True
except ImportError as _err:
    _KRONOS_AVAILABLE = False
    _IMPORT_ERR = str(_err)


# Bull Put spreads benefit from a bullish forecast (stock predicted to rise).
# Bear Call spreads benefit from a bearish forecast (stock predicted to fall).
_MULT_TABLE = [
    # (min_abs_pct, aligned_mult, opposing_mult)
    (3.0, 1.20, 0.80),
    (1.5, 1.12, 0.88),
    (0.5, 1.06, 0.94),
    (0.0, 1.00, 1.00),
]


def _kronos_mult(spread_type: str, forecast_pct: float) -> float:
    aligned = (spread_type == "Bull Put" and forecast_pct > 0) or \
              (spread_type == "Bear Call" and forecast_pct < 0)
    abs_pct = abs(forecast_pct)
    for threshold, pos_mult, neg_mult in _MULT_TABLE:
        if abs_pct >= threshold:
            return round(pos_mult if aligned else neg_mult, 2)
    return 1.00


def _write_neutral_signals(tickers: list, installed: bool = False) -> None:
    signals = {
        t: {
            "forecast_pct":           0.0,
            "direction":              "neutral",
            "kronos_mult_bull_put":   1.0,
            "kronos_mult_bear_call":  1.0,
        }
        for t in tickers
    }
    with open("data/kronos_signals.json", "w") as f:
        json.dump(
            {
                "timestamp":             datetime.now().isoformat(),
                "kronos_installed":      installed,
                "model":                 "NeoQuasar/Kronos-small" if installed else None,
                "forecast_horizon_days": 5,
                "signals":               signals,
            },
            f,
            indent=2,
        )


def main():
    print("=" * 60)
    print("STEP 01D: Kronos AI Price Forecast")
    print("=" * 60)

    with open("data/ohlcv.json") as f:
        ohlcv_data = json.load(f)

    tickers = ohlcv_data.get("tickers", [])
    ohlcv   = ohlcv_data.get("ohlcv", {})

    if not _KRONOS_AVAILABLE:
        print(f"\n⚠️  Kronos not available: {_IMPORT_ERR}")
        print("\n   One-time setup:")
        print(f"   git clone https://github.com/shiyu-coder/Kronos {KRONOS_DIR}")
        print("   pip install torch transformers huggingface_hub pandas einops safetensors tqdm")
        print("\n   Writing neutral signals (×1.00) — pipeline continues normally.")
        _write_neutral_signals(tickers, installed=False)
        print("\n✅ Step 01D complete (Kronos not installed — neutral signals)")
        return

    if torch.backends.mps.is_available():
        device = "mps"
        device_label = "M1 MPS (Apple Silicon)"
    elif torch.cuda.is_available():
        device = "cuda"
        device_label = "CUDA"
    else:
        device = "cpu"
        device_label = "CPU"

    print(f"\n   Device: {device_label}")
    print("   Loading Kronos-small from HuggingFace...")
    print("   (First run downloads ~200 MB — cached for subsequent runs)")

    tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
    model     = Kronos.from_pretrained("NeoQuasar/Kronos-small")
    model     = model.to(device)
    model.eval()

    predictor = KronosPredictor(model, tokenizer, max_context=512)

    print(f"\n   Forecasting {len(tickers)} tickers (5-day horizon)...")

    signals = {}
    failed  = []

    for ticker in tickers:
        if ticker not in ohlcv or len(ohlcv[ticker]) < 20:
            signals[ticker] = {
                "forecast_pct": 0.0, "direction": "neutral",
                "kronos_mult_bull_put": 1.0, "kronos_mult_bear_call": 1.0,
            }
            failed.append(ticker)
            continue

        try:
            rows = ohlcv[ticker]
            df   = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            df.set_index("date", inplace=True)
            df = df[["open", "high", "low", "close", "volume"]].astype(float)

            # Kronos expects pd.Series, not DatetimeIndex — .dt accessor only works on Series
            x_timestamp = pd.Series(df.index).reset_index(drop=True)
            last_date   = df.index[-1]
            y_timestamp = pd.Series(
                pd.bdate_range(start=last_date + timedelta(days=1), periods=5)
            ).reset_index(drop=True)

            input_df = df.reset_index(drop=True)

            pred_df = predictor.predict(
                df           = input_df,
                x_timestamp  = x_timestamp,
                y_timestamp  = y_timestamp,
                pred_len     = 5,
                T            = 1.0,
                top_p        = 0.9,
                sample_count = 1,
            )

            last_close     = float(df["close"].iloc[-1])
            forecast_close = float(pred_df["close"].iloc[-1])
            forecast_pct   = round((forecast_close - last_close) / last_close * 100, 2)

            direction = (
                "bullish" if forecast_pct >  0.5 else
                "bearish" if forecast_pct < -0.5 else
                "neutral"
            )

            signals[ticker] = {
                "forecast_pct":           forecast_pct,
                "direction":              direction,
                "last_close":             round(last_close, 2),
                "forecast_close_5d":      round(forecast_close, 2),
                "kronos_mult_bull_put":   _kronos_mult("Bull Put",   forecast_pct),
                "kronos_mult_bear_call":  _kronos_mult("Bear Call",  forecast_pct),
            }

            icon = "📈" if direction == "bullish" else ("📉" if direction == "bearish" else "➡️")
            print(f"   {icon} {ticker}: {forecast_pct:+.2f}% "
                  f"(${last_close:.2f} → ${forecast_close:.2f})")

        except Exception as e:
            print(f"   ⚠️  {ticker}: forecast failed — {e}")
            signals[ticker] = {
                "forecast_pct": 0.0, "direction": "neutral",
                "kronos_mult_bull_put": 1.0, "kronos_mult_bear_call": 1.0,
            }
            failed.append(ticker)

    with open("data/kronos_signals.json", "w") as f:
        json.dump(
            {
                "timestamp":             datetime.now().isoformat(),
                "kronos_installed":      True,
                "model":                 "NeoQuasar/Kronos-small",
                "forecast_horizon_days": 5,
                "signals":               signals,
            },
            f,
            indent=2,
        )

    bullish = sum(1 for s in signals.values() if s["direction"] == "bullish")
    bearish = sum(1 for s in signals.values() if s["direction"] == "bearish")
    neutral = sum(1 for s in signals.values() if s["direction"] == "neutral")
    print(f"\n   📈 Bullish: {bullish}  📉 Bearish: {bearish}  ➡️ Neutral: {neutral}")
    if failed:
        print(f"   ⚠️  {len(failed)} used neutral (no data or error): {', '.join(failed)}")

    print("\n✅ Step 01D complete: kronos_signals.json")


if __name__ == "__main__":
    main()
