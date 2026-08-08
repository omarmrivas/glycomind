"""Tests del Meal-Window Pairing Engine.

El objetivo de estos tests no es que el motor acepte ventanas, sino que **rechace las
correctas**: la metrica de producto es el ratio de ventanas validas, y un motor
permisivo lo infla con datos contaminados.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from glycomind.analysis.pairing import pair_meal
from glycomind.config import AnalysisSettings
from glycomind.domain.enums import DegradationReason, ExclusionReason, QualityFlag

CFG = AnalysisSettings()
MEAL = datetime(2026, 8, 1, 13, 20)
SESSION_START = MEAL - timedelta(days=3)


def series(
    *,
    start_offset_min: float = -60.0,
    end_offset_min: float = 240.0,
    resolution_min: int = 15,
    value: float = 95.0,
    drop_range: tuple[float, float] | None = None,
    flag_range: tuple[float, float] | None = None,
    flag: QualityFlag = QualityFlag.IMPLAUSIBLE_RATE,
):
    """Serie sintetica alrededor de MEAL, con huecos y banderas opcionales."""
    offsets = np.arange(start_offset_min, end_offset_min + resolution_min, resolution_min)
    if drop_range is not None:
        lo, hi = drop_range
        offsets = offsets[(offsets < lo) | (offsets > hi)]
    base = np.datetime64(MEAL, "s")
    ts = np.array([base + np.timedelta64(int(o * 60), "s") for o in offsets])
    vals = np.full(len(offsets), value, dtype=float)
    flags = np.zeros(len(offsets), dtype=np.int64)
    if flag_range is not None:
        lo, hi = flag_range
        flags[(offsets >= lo) & (offsets <= hi)] = flag.value
    return ts, vals, flags


def run(**kw):
    ts, vals, flags = kw.pop("data", series())
    return pair_meal(
        meal_ts=kw.pop("meal_ts", MEAL),
        readings_ts=ts,
        readings_values=vals,
        readings_flags=flags,
        resolution_min=kw.pop("resolution_min", 15),
        session_start=kw.pop("session_start", SESSION_START),
        cfg=CFG,
        **kw,
    )


def test_clean_window_is_usable():
    verdict, window = run()
    assert verdict.is_usable, verdict.exclusions
    assert window is not None
    assert verdict.effective_post_window_min == 180


def test_coarse_resolution_degrades_but_does_not_exclude():
    """15 min de resolucion es una limitacion, no un defecto: se marca, no se descarta."""
    verdict, _ = run()
    assert verdict.is_usable
    assert DegradationReason.PEAK_UNDERESTIMATED in verdict.degradations


def test_five_minute_resolution_has_no_peak_degradation():
    verdict, _ = run(data=series(resolution_min=5), resolution_min=5)
    assert verdict.is_usable
    assert DegradationReason.PEAK_UNDERESTIMATED not in verdict.degradations


def test_sensor_warmup_excludes():
    verdict, _ = run(session_start=MEAL - timedelta(hours=4))
    assert not verdict.is_usable
    assert ExclusionReason.SENSOR_WARMUP in verdict.exclusions
    assert verdict.sensor_age_hours == pytest.approx(4.0)


def test_previous_meal_too_close_excludes():
    verdict, _ = run(prev_meal_ts=MEAL - timedelta(minutes=90))
    assert not verdict.is_usable
    assert ExclusionReason.PREV_MEAL_TOO_CLOSE in verdict.exclusions
    assert verdict.prev_meal_gap_min == pytest.approx(90.0)


def test_next_meal_too_soon_excludes():
    """La comida SIGUIENTE contamina tanto como la anterior; se suele olvidar."""
    verdict, _ = run(next_meal_ts=MEAL + timedelta(minutes=90))
    assert not verdict.is_usable
    assert ExclusionReason.NEXT_MEAL_TOO_SOON in verdict.exclusions


def test_next_meal_within_180_truncates_window_instead_of_excluding():
    verdict, _ = run(next_meal_ts=MEAL + timedelta(minutes=150))
    assert verdict.is_usable
    assert DegradationReason.NEXT_MEAL_LIMITS_WINDOW in verdict.degradations
    assert verdict.effective_post_window_min == 150


def test_large_gap_excludes():
    """A 15 min el umbral es 37.5: un hueco de 60 min descarta la ventana."""
    verdict, _ = run(data=series(drop_range=(30.0, 75.0)))
    assert not verdict.is_usable
    assert ExclusionReason.GAP_TOO_LARGE in verdict.exclusions


def test_small_gap_is_tolerated():
    """Una sola muestra perdida (30 min de hueco) sigue por debajo de 37.5 min."""
    verdict, _ = run(data=series(drop_range=(29.0, 31.0)))
    assert verdict.is_usable, verdict.exclusions


def test_flagged_readings_exclude():
    verdict, _ = run(data=series(flag_range=(30.0, 60.0)))
    assert not verdict.is_usable
    assert ExclusionReason.FLAGGED_READINGS in verdict.exclusions


def test_missing_baseline_excludes():
    verdict, _ = run(data=series(start_offset_min=5.0))
    assert not verdict.is_usable
    assert ExclusionReason.NO_BASELINE in verdict.exclusions


def test_no_glucose_data_excludes():
    empty = (np.array([], dtype="datetime64[s]"), np.array([]), np.array([], dtype=np.int64))
    verdict, window = run(data=empty)
    assert not verdict.is_usable
    assert ExclusionReason.NO_GLUCOSE_DATA in verdict.exclusions
    assert window is None


def test_all_exclusion_reasons_are_collected_not_short_circuited():
    """El desglose completo es el mapa de que arreglar en la captura."""
    verdict, _ = run(
        session_start=MEAL - timedelta(hours=2),
        prev_meal_ts=MEAL - timedelta(minutes=60),
        next_meal_ts=MEAL + timedelta(minutes=60),
    )
    assert not verdict.is_usable
    assert ExclusionReason.SENSOR_WARMUP in verdict.exclusions
    assert ExclusionReason.PREV_MEAL_TOO_CLOSE in verdict.exclusions
    assert ExclusionReason.NEXT_MEAL_TOO_SOON in verdict.exclusions
    assert len(verdict.exclusions) >= 3


def test_window_extends_past_post_window_for_interpolation():
    """Hace falta una muestra mas alla de 180 para interpolar el borde sin extrapolar."""
    _, window = run()
    assert window is not None
    assert window.rel_min.max() > 180.0
