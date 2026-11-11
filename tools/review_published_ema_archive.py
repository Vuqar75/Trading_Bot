"""Replay published EMA artifacts without a new outer optimization.

Input: the authors' public wf_optim_crypto_analysis.zip, SHA pinned below.
Dependencies: numpy, pandas, pyreadr, rdata (for the original xts index).
No network access or application imports. Read R serialization, never execute R.
"""
import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyreadr
import rdata

ARCHIVE_SHA256 = "2bb40a7860733ba073a089c82b3424f4551d101d061fb31f4e9f51da978a81fd"
FEE = 0.001


def read_rds(z, name, directory):
    path = Path(directory) / "input.rds"
    path.write_bytes(z.read(name))
    return pyreadr.read_r(str(path))[None]


def verify_hourly_chain(z, prefix, directory):
    """Independent replay from saved hourly prices, not minute raw data."""
    hourly = read_rds(z, prefix + "/02_downsampled.rds", directory)
    precalc = read_rds(z, prefix + "/03_precalculated.rds", directory)
    prices = hourly.iloc[:, 0].to_numpy(float)
    assert np.isfinite(prices).all() and (prices > 0).all()
    periods = [5, 7, 10, 15, 20, 30, 40, 50, 100, 150, 200]
    emas = {}
    for period in periods:
        e = np.full(len(prices), np.nan)
        e[period - 1] = prices[:period].mean()
        alpha = 2 / (period + 1)
        for i in range(period, len(prices)):
            e[i] = alpha * prices[i] + (1 - alpha) * e[i - 1]
        emas[period] = e[199:]
        np.testing.assert_allclose(emas[period], precalc[str(period)], rtol=1e-11)
    asset = prices[199:]
    np.testing.assert_array_equal(asset, precalc["Asset"])
    columns, pos_list = [], []
    for slow in periods[6:]:
        for fast in periods[:6]:
            columns.append(f"{fast}_{slow}")
            p = np.r_[np.nan, np.where(emas[fast][:-1] > emas[slow][:-1], 1., -1.)]
            pos_list.append(p)
    positions = np.column_stack(pos_list)
    archived_positions = read_rds(z, prefix + "/04_positions.rds", directory)
    np.testing.assert_allclose(positions, archived_positions[columns], equal_nan=True)
    log_returns = np.r_[0., np.diff(np.log(asset))]
    gross = np.nan_to_num(positions * log_returns[:, None], nan=0.)
    costs = np.nan_to_num(np.abs(np.diff(positions, axis=0, prepend=np.nan)) * FEE, nan=0.)
    pnl = gross - costs
    archived_pnl = read_rds(z, prefix + "/05_pnl.rds", directory)
    np.testing.assert_allclose(pnl, archived_pnl, rtol=1e-9, atol=1e-12)
    choices = read_rds(z, prefix + "/08_wf_best_params.rds", directory)
    reproduced_test = []
    for _, choice in choices.iterrows():
        train = pnl[int(choice["train_start"]) - 1:int(choice["train_end"])]
        scores = np.sqrt(365 * 24) * train.mean(axis=0) / train.std(axis=0, ddof=1)
        selected = int(np.nanargmax(scores))
        assert "train_" + columns[selected] == choice["selected_col"]
        test = slice(int(choice["test_start"]) - 1, int(choice["test_end"]))
        reproduced_test.append(np.column_stack([pnl[test, selected], positions[test, selected]]))
    stitched = np.concatenate(reproduced_test)
    archived_test = read_rds(z, prefix + "/09_wf_test_evalued.rds", directory)
    np.testing.assert_allclose(stitched, archived_test[["return", "positions"]],
                               rtol=1e-9, atol=1e-12)
    return {"hourly_rows": len(prices), "ema_pairs": len(columns),
            "training_windows": len(choices), "replayed_test_rows": len(stitched),
            "ema_positions_pnl_and_training_selection_match": True}


def physical_costs(positions, fee=FEE):
    """Flat before the path; charge actual position changes (reverse = two)."""
    p = np.asarray(positions, dtype=float)
    return np.abs(np.diff(p, prepend=0.0)) * fee


def self_check():
    assert np.allclose(physical_costs([1, 1, -1]), [.001, 0, .002])
    # A newly selected EMA can reverse actual exposure without reversing its
    # own counterfactual position. Conversely its own reversal can overcharge.
    actual = physical_costs([1, -1, -1, 1])
    counterfactual = np.array([0, 0, .002, .002])
    assert np.allclose(actual - counterfactual, [.001, .002, -.002, 0])
    assert physical_costs([-1])[0] == .001


def sharpe(values):
    a = np.asarray(values, dtype=float)
    sd = a.std(ddof=1)
    return float(a.mean() / sd * np.sqrt(365 * 24)) if sd else None


def audit(archive):
    with open(archive, "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != ARCHIVE_SHA256:
        raise ValueError("Archive version differs; do not silently substitute")
    result = {"archive_sha256": digest, "fee_per_unit_turnover": FEE,
              "scope": "independent hourly replay; raw minute preprocessing not reproduced",
              "paths": []}
    with zipfile.ZipFile(archive) as z, tempfile.TemporaryDirectory() as temp:
        for name in sorted(z.namelist()):
            if not name.endswith("/09_wf_test_evalued.rds"):
                continue
            f = Path(temp) / "path.rds"
            f.write_bytes(z.read(name))
            frame = pyreadr.read_r(str(f))[None]
            parsed = rdata.parser.parse_file(f)
            attrs = rdata.conversion.convert(parsed.object.attributes)
            dates = pd.to_datetime(attrs["index"], unit="s", utc=True)
            assert len(dates) == len(frame) and dates.is_monotonic_increasing
            assert not dates.has_duplicates
            p = frame["positions"].to_numpy(float)
            ret = frame["return"].to_numpy(float)
            underlying = frame["Asset_returns"].to_numpy(float)
            assert np.all(np.isin(p, [-1, 1]))
            assert np.isfinite(ret).all() and np.isfinite(underlying).all()
            gross = p * underlying
            implied_cost = gross - ret
            expected = physical_costs(p)
            corrected = gross - expected
            boundary = np.r_[True, np.diff(frame["test_start"].to_numpy()) != 0]
            discrepancy = np.abs(implied_cost - expected) > 1e-10
            metadata_text = z.read(name.rsplit("/", 1)[0] + "/metrics.yaml").decode()
            metadata = dict(line.split(": ", 1) for line in metadata_text.splitlines()
                            if ": " in line)
            start = pd.Timestamp(metadata["start"].removeprefix("start "), tz="UTC")
            end = pd.Timestamp(metadata["end"].removeprefix("end "), tz="UTC")
            clip = (dates >= start) & (dates <= end)
            author_sr = sharpe(ret[clip])
            published_sr = float(metadata["sharpe_ratio"])
            assert abs(author_sr - published_sr) < 1e-5, (name, author_sr, published_sr)
            assert abs(sharpe(ret) - float(metadata["sharpe_ratio_full"])) < 1e-5
            # Cost-only correction preserves every selected position and the
            # authors' signed-log-return convention; not a futures cash ledger.
            row = {"path": name, "rows": len(frame),
                   "independent_hourly_chain": verify_hourly_chain(z, name.rsplit("/", 1)[0], temp),
                   "start_utc": str(dates[0]), "end_utc": str(dates[-1]),
                   "original_rds_sha256": hashlib.sha256(z.read(name)).hexdigest(),
                   "hourly_input_sha256": hashlib.sha256(z.read(name.rsplit("/", 1)[0]
                                                                  + "/02_downsampled.rds")).hexdigest(),
                   "published_clipped_sharpe": published_sr,
                   "recalculated_clipped_sharpe": author_sr,
                   "physical_cost_clipped_sharpe": sharpe(corrected[clip]),
                   "published_full_sharpe": float(metadata["sharpe_ratio_full"]),
                   "physical_cost_full_sharpe": sharpe(corrected),
                   "author_full_log_sum": float(ret.sum()),
                   "physical_cost_full_log_sum": float(corrected.sum()),
                   "implied_author_cost_sum": float(implied_cost.sum()),
                   "physical_cost_sum_before_final_close": float(expected.sum()),
                   "additional_final_close_cost": FEE * float(abs(p[-1])),
                   "cost_mismatch_rows": int(discrepancy.sum()),
                   "mismatches_on_window_boundaries": int((discrepancy & boundary).sum()),
                   "mismatches_outside_boundaries": int((discrepancy & ~boundary).sum()),
                   "hour_grid_breaks": int((np.diff(attrs["index"]) != 3600).sum())}
            result["paths"].append(row)
    assert len(result["paths"]) == 8
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    self_check()
    args.output.write_text(json.dumps(audit(args.archive), indent=2) + "\n")
