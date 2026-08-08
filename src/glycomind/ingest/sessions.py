"""Deteccion de sesiones de sensor.

Una sesion = un sensor fisico. Es la unidad de correccion de sesgo mas importante del
sistema: el escalon entre sensores es el mayor artefacto sistematico del CGM, y sin
delimitar sesiones no se puede excluir el warm-up ni tratarlo como efecto aleatorio en
el modelo jerarquico de la Fase 2.

En LibreView la delimitacion es facil y fiable porque el CSV trae el numero de serie del
dispositivo en cada fila. En fuentes que no lo expongan habria que inferirla por huecos,
que es mucho peor.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from glycomind.analysis.metrics import detect_resolution_min
from glycomind.domain.models import RawReading


@dataclass(frozen=True, slots=True)
class DetectedSession:
    device_serial_hash: str
    started_at: datetime
    ended_at: datetime
    n_readings: int
    native_resolution_min: int


def detect_sessions(readings: Sequence[RawReading]) -> list[DetectedSession]:
    """Agrupa lecturas del historico continuo en sesiones por numero de serie.

    Solo se usa el historico: los escaneos caen en instantes arbitrarios y corromperian
    la deteccion de resolucion nativa.
    """
    buckets: dict[str, list[RawReading]] = defaultdict(list)
    for r in readings:
        if r.source_record == "historic":
            buckets[r.device_serial_hash].append(r)

    sessions: list[DetectedSession] = []
    for serial, items in buckets.items():
        items.sort(key=lambda r: r.ts_utc)
        ts = np.array([np.datetime64(r.ts_utc.replace(tzinfo=None), "s") for r in items])
        try:
            resolution = detect_resolution_min(ts)
        except ValueError:
            # Sensor con una sola lectura utilizable: no se puede caracterizar.
            continue
        sessions.append(
            DetectedSession(
                device_serial_hash=serial,
                started_at=items[0].ts_utc,
                ended_at=items[-1].ts_utc,
                n_readings=len(items),
                native_resolution_min=resolution,
            )
        )
    return sorted(sessions, key=lambda s: s.started_at)
