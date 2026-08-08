"""Control de calidad de la senal CGM.

Principio: **se marca, nunca se borra.** La politica de exclusion vive en la consulta,
no en la ingesta, para poder cambiarla sin reimportar. Cada bandera es una hipotesis
falsable sobre un artefacto conocido del sensor.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from glycomind.config import AnalysisSettings
from glycomind.domain.enums import QualityFlag


@dataclass(frozen=True, slots=True)
class QualityReport:
    flags: np.ndarray  # int64, un bitmask por lectura
    n_out_of_range: int
    n_implausible_rate: int
    n_warmup: int
    n_compression_low: int


def assess_session(
    ts_utc: np.ndarray,
    values: np.ndarray,
    *,
    session_start: np.datetime64,
    resolution_min: int,
    cfg: AnalysisSettings,
    base_flags: np.ndarray | None = None,
) -> QualityReport:
    """Evalua la calidad de una sesion de sensor completa."""
    n = len(values)
    flags = (
        np.zeros(n, dtype=np.int64) if base_flags is None else base_flags.astype(np.int64).copy()
    )

    # 1. Rango fisiologico. Fuera de 40-400 mg/dL no es glucosa, es fallo del sensor.
    oor = (values < cfg.physiological_min_mgdl) | (values > cfg.physiological_max_mgdl)
    flags[oor] |= QualityFlag.OUT_OF_PHYSIOLOGICAL_RANGE.value

    # 2. Tasa de cambio imposible. Solo entre muestras contiguas: a traves de un hueco la
    #    "tasa" no significa nada.
    minutes = (
        ts_utc.astype("datetime64[s]").astype(np.int64)
        - int(ts_utc[0].astype("datetime64[s]").astype(np.int64))
    ) / 60.0
    if n >= 2:
        dt = np.diff(minutes)
        dv = np.abs(np.diff(values))
        contiguous = dt <= 2 * resolution_min
        with np.errstate(divide="ignore", invalid="ignore"):
            rate = np.where(dt > 0, dv / dt, 0.0)
        bad = contiguous & (rate > cfg.max_rate_mgdl_per_min)
        flags[1:][bad] |= QualityFlag.IMPLAUSIBLE_RATE.value

    # 3. Warm-up de analisis. NO es el warm-up del fabricante (~1 h): responde al error
    #    elevado documentado durante las primeras horas de vida del sensor.
    warmup_end = session_start + np.timedelta64(int(cfg.analysis_warmup_hours * 60), "m")
    flags[ts_utc < warmup_end] |= QualityFlag.IN_SENSOR_WARMUP.value

    # 4. Compression lows: caida brusca y recuperacion brusca por dormir sobre el sensor.
    comp = _detect_compression_lows(minutes, values, cfg)
    flags[comp] |= QualityFlag.SUSPECTED_COMPRESSION_LOW.value

    return QualityReport(
        flags=flags,
        n_out_of_range=int(np.count_nonzero(oor)),
        n_implausible_rate=int(np.count_nonzero(flags & QualityFlag.IMPLAUSIBLE_RATE.value)),
        n_warmup=int(np.count_nonzero(ts_utc < warmup_end)),
        n_compression_low=int(np.count_nonzero(comp)),
    )


def _detect_compression_lows(
    minutes: np.ndarray, values: np.ndarray, cfg: AnalysisSettings
) -> np.ndarray:
    """Detecta descensos en V: caida rapida seguida de recuperacion rapida.

    Una hipoglucemia real no se recupera sola en 30 minutos sin ingesta. Una compresion
    mecanica del sensor si. Confundirlas contamina tanto las metricas nocturnas como
    cualquier futura alerta.
    """
    n = len(values)
    mask = np.zeros(n, dtype=bool)
    if n < 3:
        return mask

    for j in range(1, n - 1):
        if not (values[j] <= values[j - 1] and values[j] <= values[j + 1]):
            continue
        if values[j] >= 80.0:
            continue

        pre = (minutes >= minutes[j] - cfg.compression_drop_window_min) & (minutes < minutes[j])
        post = (minutes > minutes[j]) & (minutes <= minutes[j] + cfg.compression_recovery_min)
        if not pre.any() or not post.any():
            continue

        drop = float(values[pre].max()) - float(values[j])
        recovery = float(values[post].max()) - float(values[j])
        if drop >= cfg.compression_drop_mgdl and recovery >= 0.7 * cfg.compression_drop_mgdl:
            span = (minutes >= minutes[pre][np.argmax(values[pre])]) & (
                minutes <= minutes[post][np.argmax(values[post])]
            )
            mask |= span
    return mask


def detect_session_step(
    prev_tail: np.ndarray, next_head: np.ndarray, cfg: AnalysisSettings
) -> float | None:
    """Escalon sistematico entre el final de un sensor y el inicio del siguiente.

    Es la mayor fuente de artefacto sistematico en CGM: dos sensores del mismo modelo
    pueden diferir de forma consistente. Se cuantifica para poder tratarlo como efecto
    aleatorio en el modelo jerarquico (Fase 2).
    """
    if prev_tail.size == 0 or next_head.size == 0:
        return None
    step = float(np.median(next_head) - np.median(prev_tail))
    return step if abs(step) >= cfg.sensor_step_threshold_mgdl else None
