"""Meal-Window Pairing Engine.

Responde a una sola pregunta: *¿esta ventana de glucosa es atribuible a esta comida?*
En la vida real la respuesta es que **no** muchas mas veces de lo que la gente espera, y
el sistema tiene que decirlo en vez de calcular metricas sobre datos contaminados.

El componente es puro (no toca la base de datos) para que sea testeable sin infra, y
**acumula todas** las razones de exclusion en lugar de cortocircuitar en la primera: el
desglose de por que se descartan ventanas es el mapa de que hay que arreglar en la UX de
registro.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from glycomind.analysis.metrics import compute_coverage
from glycomind.config import AnalysisSettings
from glycomind.domain.enums import DegradationReason, ExclusionReason
from glycomind.domain.models import PairingVerdict, WindowSeries


def _to_np(dt: datetime) -> np.datetime64:
    return np.datetime64(dt.replace(tzinfo=None), "s")


def build_window(
    *,
    meal_ts: datetime,
    readings_ts: np.ndarray,
    readings_values: np.ndarray,
    readings_flags: np.ndarray,
    resolution_min: int,
    cfg: AnalysisSettings,
    post_window_min: int,
) -> WindowSeries:
    """Extrae la ventana en minutos relativos a la comida.

    Se toma **una muestra extra mas alla** de ``post_window_min`` para que el valor en el
    borde exacto pueda interpolarse en vez de extrapolarse.
    """
    meal_np = _to_np(meal_ts)
    rel = (readings_ts.astype("datetime64[s]") - meal_np).astype("timedelta64[s]").astype(
        np.int64
    ) / 60.0
    lo = -float(cfg.pre_window_min(resolution_min))
    hi = float(post_window_min + resolution_min + 1)
    m = (rel >= lo) & (rel <= hi)
    return WindowSeries(
        rel_min=rel[m],
        values=readings_values[m],
        flags=readings_flags[m],
        resolution_min=resolution_min,
    )


def pair_meal(
    *,
    meal_ts: datetime,
    readings_ts: np.ndarray,
    readings_values: np.ndarray,
    readings_flags: np.ndarray,
    resolution_min: int,
    session_start: datetime | None,
    prev_meal_ts: datetime | None = None,
    next_meal_ts: datetime | None = None,
    vendor_changed_within_window: bool = False,
    cfg: AnalysisSettings | None = None,
) -> tuple[PairingVerdict, WindowSeries | None]:
    """Decide si la ventana postprandial es utilizable y con que calidad."""
    cfg = cfg or AnalysisSettings()
    exclusions: list[ExclusionReason] = []
    degradations: list[DegradationReason] = []

    # --- contexto temporal ---
    prev_gap = _gap_min(prev_meal_ts, meal_ts)
    next_gap = _gap_min(meal_ts, next_meal_ts)
    sensor_age_h = (meal_ts - session_start).total_seconds() / 3600.0 if session_start else None

    if sensor_age_h is not None and sensor_age_h < cfg.analysis_warmup_hours:
        exclusions.append(ExclusionReason.SENSOR_WARMUP)
    if prev_gap is not None and prev_gap < cfg.exclude_if_prev_meal_within_min:
        exclusions.append(ExclusionReason.PREV_MEAL_TOO_CLOSE)
    if vendor_changed_within_window:
        exclusions.append(ExclusionReason.VENDOR_CHANGE)

    # La comida SIGUIENTE contamina tanto como la anterior. Se suele olvidar.
    effective_post = cfg.post_window_min
    if next_gap is not None:
        if next_gap < cfg.exclude_if_next_meal_within_min:
            exclusions.append(ExclusionReason.NEXT_MEAL_TOO_SOON)
        elif next_gap < cfg.degrade_if_next_meal_within_min:
            effective_post = int(next_gap)
            degradations.append(DegradationReason.NEXT_MEAL_LIMITS_WINDOW)

    # --- ventana de datos ---
    if readings_ts.size == 0:
        return (
            _verdict(
                False,
                [*exclusions, ExclusionReason.NO_GLUCOSE_DATA],
                degradations,
                prev_gap,
                next_gap,
                effective_post,
                sensor_age_h,
            ),
            None,
        )

    window = build_window(
        meal_ts=meal_ts,
        readings_ts=readings_ts,
        readings_values=readings_values,
        readings_flags=readings_flags,
        resolution_min=resolution_min,
        cfg=cfg,
        post_window_min=effective_post,
    )

    if len(window) < 3:
        exclusions.append(ExclusionReason.NO_GLUCOSE_DATA)
    else:
        coverage, max_gap = compute_coverage(window, cfg, effective_post)
        if coverage < cfg.min_coverage_pct:
            exclusions.append(ExclusionReason.INSUFFICIENT_COVERAGE)
        if max_gap > cfg.max_gap_min(resolution_min):
            exclusions.append(ExclusionReason.GAP_TOO_LARGE)
        if window.has_flagged:
            exclusions.append(ExclusionReason.FLAGGED_READINGS)

        # Sin basal no hay iAUC, y un iAUC sin basal fiable es peor que ninguno.
        width = cfg.baseline_window_min(resolution_min)
        pre = window.slice(-(cfg.baseline_offset_min + width), -cfg.baseline_offset_min)
        if len(pre) == 0:
            exclusions.append(ExclusionReason.NO_BASELINE)
        elif len(pre) == 1:
            degradations.append(DegradationReason.WEAK_BASELINE)

    if resolution_min > cfg.peak_reliable_max_resolution_min:
        degradations.append(DegradationReason.PEAK_UNDERESTIMATED)

    usable = not exclusions
    return (
        _verdict(
            usable, exclusions, degradations, prev_gap, next_gap, effective_post, sensor_age_h
        ),
        window if len(window) >= 3 else None,
    )


def _gap_min(earlier: datetime | None, later: datetime | None) -> float | None:
    if earlier is None or later is None:
        return None
    return (later - earlier).total_seconds() / 60.0


def _verdict(
    usable: bool,
    exclusions: list[ExclusionReason],
    degradations: list[DegradationReason],
    prev_gap: float | None,
    next_gap: float | None,
    effective_post: int,
    sensor_age_h: float | None,
) -> PairingVerdict:
    return PairingVerdict(
        is_usable=usable,
        exclusions=tuple(dict.fromkeys(exclusions)),
        degradations=tuple(dict.fromkeys(degradations)),
        prev_meal_gap_min=prev_gap,
        next_meal_gap_min=next_gap,
        effective_post_window_min=effective_post,
        sensor_age_hours=sensor_age_h,
    )
