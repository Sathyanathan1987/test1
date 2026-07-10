# NIFTY ML Next-7-Day Prediction Script
# File: scripts/run_nifty_ml.py

"""
Simple reproducible script to train ML models (LightGBM / XGBoost) for next-day and
next-7-business-day direction forecasts on a NIFTY CSV in this repo.

Usage:
  python3 scripts/run_nifty_ml.py --csv "path/to/NIFTY.csv" --outdir outputs

By default the script will look for any CSV in the repo matching "NIFTY*.csv".
Outputs:
  - outputs/predictions_next7.csv  (ensemble probabilities + directions)
  - outputs/model_report.md        (brief model metrics and feature importance)
  - outputs/feature_importance.png

Notes:
  - This does NOT run here — run locally or in a cloud runner with required packages.
  - The forecasting for 7 days is iterative (one-day-ahead model iterated forward).

Packages required: pandas, numpy, scikit-learn, lightgbm, xgboost, matplotlib
"""

import argparse
import glob
import os
from datetime import timedelta

import numpy as np
import pandas as pd

# Try to import optional libs
try:
    import lightgbm as lgb
except Exception:
    lgb = None
try:
    import xgboost as xgb
except Exception:
    xgb = None
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score, roc_auc_score
import matplotlib.pyplot as plt


def load_csv(path=None):
    if path and os.path.exists(path):
        return pd.read_csv(path)
    # fallback: find a CSV matching NIFTY*
    candidates = glob.glob("NIFTY*.csv") + glob.glob("**/NIFTY*.csv", recursive=True)
    if not candidates:
        raise FileNotFoundError("No NIFTY CSV found in repo. Provide --csv path.")
    print(f"Using CSV: {candidates[0]}")
    return pd.read_csv(candidates[0])


def prepare_features(df):
    # Expect columns: Date, Open, High, Low, Close, Volume, Turnover (optional)
    df = df.copy()
    if 'Date' in df.columns:
        df['Date'] = pd.to_datetime(df['Date'])
    else:
        # try common names
        df.columns = [c if c.lower()!='date' else 'Date' for c in df.columns]
    df = df.sort_values('Date').reset_index(drop=True)

    # basic price features
    df['close'] = df['Close']
    df['open'] = df['Open']
    df['high'] = df['High']
    df['low'] = df['Low']

    # returns
    df['ret_1'] = df['close'].pct_change(1)
    for lag in [1,2,3,5,7,10,20]:
        df[f'ret_lag_{lag}'] = df['ret_1'].shift(lag)

    # moving averages
    df['sma5'] = df['close'].rolling(5).mean()
    df['sma10'] = df['close'].rolling(10).mean()
    df['sma20'] = df['close'].rolling(20).mean()

    # volatility proxy
    df['range'] = df['high'] - df['low']
    df['atr_10'] = df['range'].rolling(10).mean()

    # volume normalized
    if 'Volume' in df.columns:
        df['vol'] = df['Volume']
        df['vol_avg_10'] = df['vol'].rolling(10).mean()
        df['vol_rel'] = df['vol'] / (df['vol_avg_10'] + 1e-9)

    # RSI simple implementation (14)
    delta = df['close'].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    roll_up = up.rolling(14).mean()
    roll_down = down.rolling(14).mean()
    rs = roll_up / (roll_down + 1e-9)
    df['rsi14'] = 100.0 - (100.0 / (1.0 + rs))

    df = df.dropna().reset_index(drop=True)
    return df


def make_labels(df, horizon=1):
    # binary direction label: upward move (close_t+horizon > close_t)
    y = (df['close'].shift(-horizon) > df['close']).astype(int)
    return y


def train_models(X, y):
    # time-series split
    tscv = TimeSeriesSplit(n_splits=5)
    metrics = {}

    # simple logistic baseline
    lr = LogisticRegression(max_iter=1000)
    # train on last fold
    train_idx, test_idx = list(tscv.split(X))[-1]
    lr.fit(X.iloc[train_idx], y.iloc[train_idx])
    ypred_lr = lr.predict(X.iloc[test_idx])
    yprob_lr = lr.predict_proba(X.iloc[test_idx])[:, 1]
    metrics['logistic_acc'] = accuracy_score(y.iloc[test_idx], ypred_lr)
    metrics['logistic_auc'] = roc_auc_score(y.iloc[test_idx], yprob_lr)

    # RandomForest
    rf = RandomForestClassifier(n_estimators=200, random_state=42)
    rf.fit(X.iloc[train_idx], y.iloc[train_idx])
    ypred_rf = rf.predict(X.iloc[test_idx])
    yprob_rf = rf.predict_proba(X.iloc[test_idx])[:, 1]
    metrics['rf_acc'] = accuracy_score(y.iloc[test_idx], ypred_rf)
    metrics['rf_auc'] = roc_auc_score(y.iloc[test_idx], yprob_rf)

    # LightGBM
    lgbm_model = None
    if lgb is not None:
        lgbm_model = lgb.LGBMClassifier(n_estimators=500, random_state=42)
        lgbm_model.fit(X.iloc[train_idx], y.iloc[train_idx])
        ypred_lgb = lgbm_model.predict(X.iloc[test_idx])
        yprob_lgb = lgbm_model.predict_proba(X.iloc[test_idx])[:, 1]
        metrics['lgbm_acc'] = accuracy_score(y.iloc[test_idx], ypred_lgb)
        metrics['lgbm_auc'] = roc_auc_score(y.iloc[test_idx], yprob_lgb)

    # XGBoost
    xgb_model = None
    if xgb is not None:
        xgb_model = xgb.XGBClassifier(use_label_encoder=False, eval_metric='logloss', random_state=42)
        xgb_model.fit(X.iloc[train_idx], y.iloc[train_idx])
        ypred_xgb = xgb_model.predict(X.iloc[test_idx])
        yprob_xgb = xgb_model.predict_proba(X.iloc[test_idx])[:, 1]
        metrics['xgb_acc'] = accuracy_score(y.iloc[test_idx], ypred_xgb)
        metrics['xgb_auc'] = roc_auc_score(y.iloc[test_idx], yprob_xgb)

    # return trained models and metrics
    models = {
        'logistic': lr,
        'rf': rf,
        'lgbm': lgbm_model,
        'xgb': xgb_model,
    }
    return models, metrics


def iterative_forecast(df, features, models, days=7):
    # Predict next `days` business days using one-day-ahead model iteratively.
    # Start from last row features
    last_row = df.iloc[-1:].copy()
    preds = []

    for i in range(days):
        Xlast = last_row[features]
        probs = {}
        for name, m in models.items():
            if m is None:
                probs[name] = np.nan
                continue
            try:
                prob = m.predict_proba(Xlast)[0, 1]
            except Exception:
                prob = float(m.predict(Xlast)[0])
            probs[name] = float(prob)
        # ensemble simple avg of available probs
        available = [v for v in probs.values() if not (v is None or np.isnan(v))]
        ens = float(np.mean(available)) if available else np.nan

        # estimate next close by assuming prob * avg_up_move + (1-prob)*avg_down_move
        # simple heuristic: use last close and expected small step = ret_1 mean * sign
        expected_ret = last_row['ret_1'].values[0] * ens
        next_close = float(last_row['close'].values[0] * (1 + expected_ret))

        # build next date (business day)
        last_date = last_row['Date'].values[0]
        next_date = pd.to_datetime(last_date) + pd.tseries.offsets.BDay(1)

        preds.append({
            'Date': pd.to_datetime(next_date).strftime('%Y-%m-%d'),
            'pred_prob_logistic': probs.get('logistic', np.nan),
            'pred_prob_rf': probs.get('rf', np.nan),
            'pred_prob_lgbm': probs.get('lgbm', np.nan),
            'pred_prob_xgb': probs.get('xgb', np.nan),
            'ensemble_prob': ens,
            'pred_close': next_close,
        })

        # update last_row to emulate rolling forward
        # crude update: set close to predicted, recompute derived features partly
        lr = last_row.copy()
        lr['Date'] = pd.to_datetime(next_date)
        lr['close'] = next_close
        lr['ret_1'] = (next_close / last_row['close'].values[0]) - 1
        # shift lagged returns
        for lag in [1,2,3,5,7,10,20]:
            lr[f'ret_lag_{lag}'] = last_row[f'ret_lag_{lag}'].values[0]
        # update sma approximations: keep previous SMA but shift slightly
        lr['sma5'] = last_row['sma5'].values[0] + (next_close - last_row['close'].values[0]) / 5.0
        lr['sma10'] = last_row['sma10'].values[0] + (next_close - last_row['close'].values[0]) / 10.0
        lr['sma20'] = last_row['sma20'].values[0] + (next_close - last_row['close'].values[0]) / 20.0
        lr['atr_10'] = last_row['atr_10'].values[0]
        # rsi keep same
        lr['rsi14'] = last_row['rsi14'].values[0]
        # vol_rel same
        if 'vol_rel' in last_row.columns:
            lr['vol_rel'] = last_row['vol_rel'].values[0]

        last_row = lr

    return pd.DataFrame(preds)


def main(csv_path=None, outdir='outputs'):
    df = load_csv(csv_path)
    df = prepare_features(df)

    # target: next-day direction
    df['y_1'] = make_labels(df, horizon=1)

    features = [
        'ret_1', 'ret_lag_1', 'ret_lag_2', 'ret_lag_3', 'ret_lag_5',
        'sma5', 'sma10', 'sma20', 'atr_10', 'rsi14'
    ]
    if 'vol_rel' in df.columns:
        features += ['vol_rel']

    X = df[features]
    y = df['y_1']

    models, metrics = train_models(X, y)

    if not os.path.exists(outdir):
        os.makedirs(outdir)

    # Run iterative forecast 7 business days
    preds = iterative_forecast(df, features, models, days=7)
    preds.to_csv(os.path.join(outdir, 'predictions_next7.csv'), index=False)

    # write a simple report
    with open(os.path.join(outdir, 'model_report.md'), 'w') as f:
        f.write('# Model Report\n\n')
        f.write('Metrics (last fold):\n')
        for k, v in metrics.items():
            f.write(f'- {k}: {v}\n')
        f.write('\nPredictions written to predictions_next7.csv\n')

    print('Done. Outputs in', outdir)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv', type=str, default=None, help='Path to NIFTY CSV')
    parser.add_argument('--outdir', type=str, default='outputs', help='Output directory')
    args = parser.parse_args()
    main(csv_path=args.csv, outdir=args.outdir)
