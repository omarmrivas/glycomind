"""Orquestacion: comida -> ventana -> metricas -> meal_glucose_response.

Produce la tabla central del producto. Todo lo que se construya despues (modelo
jerarquico, claims personales, recomendaciones) se apoya en ella; si esta sucia, nada de
lo de arriba vale nada.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from glycomind.analysis.metrics import compute_metrics
from glycomind.analysis.pairing import pair_meal
from glycomind.config import ALGORITHM_VERSION, AnalysisSettings, settings
from glycomind.db.models import CgmSensorSession, GlucoseReading, Meal, MealGlucoseResponse
from glycomind.domain.enums import DegradationReason, ResponseQuality

# Degradaciones que son propiedad de la FUENTE, no de la ventana concreta.
# El pico subestimado ocurre en el 100% de las ventanas de un sensor a 15 min: si
# rebajara la calidad, "degraded" dejaria de distinguir nada. Se informa en la metrica
# (``peak_underestimated``) y se muestra al usuario, pero no penaliza la ventana.
_INFORMATIONAL_DEGRADATIONS = frozenset({DegradationReason.PEAK_UNDERESTIMATED})


@dataclass(frozen=True, slots=True)
class AnalysisSummary:
    meals_total: int
    computed: int
    usable: int
    degraded: int
    excluded: int
    skipped_existing: int
    exclusion_counts: dict[str, int]

    @property
    def pairing_valid_ratio(self) -> float:
        """LA metrica de producto de la Fase 1.

        Si cae por debajo de ~60%, el problema es la UX de registro y la adherencia al
        sensor, no la estadistica. Ningun modelo arregla eso.
        """
        return self.usable / self.computed if self.computed else 0.0


@dataclass(frozen=True, slots=True)
class _SessionData:
    id: uuid.UUID
    vendor: str
    started_at: datetime
    ended_at: datetime
    resolution_min: int
    ts: np.ndarray
    values: np.ndarray
    flags: np.ndarray


def analyze_user(
    db: Session,
    *,
    user_id: uuid.UUID,
    recompute: bool = False,
    cfg: AnalysisSettings | None = None,
) -> AnalysisSummary:
    cfg = cfg or settings.analysis
    sessions = _load_sessions(db, user_id)
    meals = list(
        db.execute(select(Meal).where(Meal.user_id == user_id).order_by(Meal.consumed_at)).scalars()
    )
    existing = {
        r.meal_id: r
        for r in db.execute(
            select(MealGlucoseResponse).where(MealGlucoseResponse.user_id == user_id)
        ).scalars()
    }

    computed = usable = degraded = excluded = skipped = 0
    exclusion_counts: dict[str, int] = {}

    for i, meal in enumerate(meals):
        prior = existing.get(meal.id)
        if prior is not None and not recompute and prior.algorithm_version == ALGORITHM_VERSION:
            skipped += 1
            continue
        if prior is not None:
            db.execute(delete(MealGlucoseResponse).where(MealGlucoseResponse.id == prior.id))

        prev_ts = meals[i - 1].consumed_at if i > 0 else None
        next_ts = meals[i + 1].consumed_at if i + 1 < len(meals) else None
        row = _analyze_meal(meal, sessions, prev_ts, next_ts, cfg)
        db.add(row)
        computed += 1

        if row.quality == ResponseQuality.EXCLUDED.value:
            excluded += 1
            for reason in row.exclusion_reasons:
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
        else:
            usable += 1
            if row.quality == ResponseQuality.DEGRADED.value:
                degraded += 1

    return AnalysisSummary(
        meals_total=len(meals),
        computed=computed,
        usable=usable,
        degraded=degraded,
        excluded=excluded,
        skipped_existing=skipped,
        exclusion_counts=dict(sorted(exclusion_counts.items(), key=lambda kv: -kv[1])),
    )


def _analyze_meal(
    meal: Meal,
    sessions: Sequence[_SessionData],
    prev_ts: datetime | None,
    next_ts: datetime | None,
    cfg: AnalysisSettings,
) -> MealGlucoseResponse:
    meal_ts = _aware(meal.consumed_at)
    sess = _session_for(sessions, meal_ts)

    if sess is None:
        empty = np.array([], dtype="datetime64[s]")
        verdict, _ = pair_meal(
            meal_ts=meal_ts,
            readings_ts=empty,
            readings_values=np.array([]),
            readings_flags=np.array([], dtype=np.int64),
            resolution_min=15,
            session_start=None,
            prev_meal_ts=_aware_or_none(prev_ts),
            next_meal_ts=_aware_or_none(next_ts),
            cfg=cfg,
        )
        return _build_row(meal, None, verdict, None, None)

    verdict, window = pair_meal(
        meal_ts=meal_ts,
        readings_ts=sess.ts,
        readings_values=sess.values,
        readings_flags=sess.flags,
        resolution_min=sess.resolution_min,
        session_start=sess.started_at,
        prev_meal_ts=_aware_or_none(prev_ts),
        next_meal_ts=_aware_or_none(next_ts),
        vendor_changed_within_window=_vendor_changed(sessions, meal_ts, sess.vendor, cfg),
        cfg=cfg,
    )

    metrics = None
    if window is not None:
        metrics = compute_metrics(window, cfg, post_window_min=verdict.effective_post_window_min)
    return _build_row(meal, sess, verdict, metrics, sess.vendor)


def _build_row(meal, sess, verdict, metrics, vendor) -> MealGlucoseResponse:
    penalizing = [d for d in verdict.degradations if d not in _INFORMATIONAL_DEGRADATIONS]
    if not verdict.is_usable:
        quality = ResponseQuality.EXCLUDED
    elif penalizing or metrics is None:
        quality = ResponseQuality.DEGRADED
    else:
        quality = ResponseQuality.OK

    local = _aware(meal.consumed_at) + timedelta(minutes=meal.tz_offset_min)
    row = MealGlucoseResponse(
        id=uuid.uuid4(),
        meal_id=meal.id,
        user_id=meal.user_id,
        session_id=sess.id if sess else None,
        quality=quality.value,
        exclusion_reasons=[e.value for e in verdict.exclusions],
        degradation_reasons=[d.value for d in verdict.degradations],
        hour_local=local.hour,
        prev_meal_gap_min=verdict.prev_meal_gap_min,
        next_meal_gap_min=verdict.next_meal_gap_min,
        sensor_age_hours=verdict.sensor_age_hours,
        vendor=vendor,
        algorithm_version=ALGORITHM_VERSION,
    )
    if metrics is not None:
        row.baseline_mgdl = metrics.baseline_mgdl
        row.baseline_sd = metrics.baseline_sd
        row.baseline_n = metrics.baseline_n
        row.peak_mgdl = metrics.peak_mgdl
        row.peak_delta_mgdl = metrics.peak_delta_mgdl
        row.time_to_peak_min = metrics.time_to_peak_min
        row.peak_underestimated = metrics.peak_underestimated
        row.iauc_120 = metrics.iauc_120
        row.iauc_180 = metrics.iauc_180
        row.iauc_net_120 = metrics.iauc_net_120
        row.auc_total_120 = metrics.auc_total_120
        row.time_above_baseline_min = metrics.time_above_baseline_min
        row.time_to_return_baseline_min = metrics.time_to_return_baseline_min
        row.cv_pct = metrics.cv_pct
        row.curve_shape = metrics.curve_shape.value
        row.coverage_pct = metrics.coverage_pct
        row.max_gap_min = metrics.max_gap_min
        row.n_points = metrics.n_points
        row.resolution_min = metrics.resolution_min
    return row


def _load_sessions(db: Session, user_id: uuid.UUID) -> list[_SessionData]:
    out: list[_SessionData] = []
    rows = db.execute(
        select(CgmSensorSession)
        .where(CgmSensorSession.user_id == user_id)
        .order_by(CgmSensorSession.started_at)
    ).scalars()
    for s in rows:
        readings = db.execute(
            select(GlucoseReading.ts_utc, GlucoseReading.value_mgdl, GlucoseReading.quality_flags)
            .where(
                GlucoseReading.session_id == s.id,
                GlucoseReading.source_record == "historic",
            )
            .order_by(GlucoseReading.ts_utc)
        ).all()
        if not readings:
            continue
        out.append(
            _SessionData(
                id=s.id,
                vendor=s.vendor,
                started_at=_aware(s.started_at),
                ended_at=_aware(s.ended_at or s.started_at),
                resolution_min=s.native_resolution_min or 15,
                ts=np.array([np.datetime64(r[0].replace(tzinfo=None), "s") for r in readings]),
                values=np.array([float(r[1]) for r in readings]),
                flags=np.array([int(r[2]) for r in readings], dtype=np.int64),
            )
        )
    return out


def _session_for(sessions: Sequence[_SessionData], ts: datetime) -> _SessionData | None:
    for s in sessions:
        if s.started_at <= ts <= s.ended_at:
            return s
    return None


def _vendor_changed(
    sessions: Sequence[_SessionData], ts: datetime, vendor: str, cfg: AnalysisSettings
) -> bool:
    """Un cambio de marca invalida la comparabilidad.

    El CV del iAUC-2h entre marcas es ~12.5% frente a ~3.7% dentro de la misma marca:
    mezclarlas fabrica diferencias que no existen.
    """
    delta = timedelta(hours=cfg.exclude_if_vendor_changed_within_h)
    return any(
        s.vendor != vendor and s.started_at - delta <= ts <= s.ended_at + delta for s in sessions
    )


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _aware_or_none(dt: datetime | None) -> datetime | None:
    return _aware(dt) if dt else None
