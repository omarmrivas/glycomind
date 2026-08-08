"""Objetos de valor del dominio. Inmutables, sin dependencias de base de datos."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from glycomind.domain.enums import CurveShape, DegradationReason, ExclusionReason, QualityFlag


@dataclass(frozen=True, slots=True)
class RawReading:
    """Una lectura de glucosa tal como sale del adaptador, antes de persistir.

    ``ts_utc`` siempre es tiempo absoluto. ``tz_offset_min`` se guarda aparte porque el
    analisis del efecto de la hora del dia necesita hora *local*, y reconstruirla desde
    UTC despues es una fuente de errores silenciosos.
    """

    ts_utc: datetime
    tz_offset_min: int
    value_mgdl: float
    vendor: str
    device_serial_hash: str
    source_record: str
    """'historic' | 'scan' | 'strip' — la procedencia dentro del propio export."""


@dataclass(frozen=True, slots=True)
class RawFoodEntry:
    """Registro de comida/carbohidratos hecho por el usuario dentro de la app del sensor.

    El CSV de LibreView los incluye. Glooko los descarta; nosotros no, porque son un
    registro de comidas con friccion cero (aunque sin foto ni descripcion rica).
    """

    ts_utc: datetime
    tz_offset_min: int
    carbs_grams: float | None
    carbs_servings: float | None
    note: str | None


@dataclass(frozen=True, slots=True)
class ImportResult:
    vendor: str
    rows_parsed: int
    readings_found: int
    readings_inserted: int
    readings_duplicate: int
    food_entries_found: int
    food_entries_inserted: int
    sessions_created: int
    first_ts_utc: datetime | None
    last_ts_utc: datetime | None
    detected_resolution_min: int | None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GlucoseSeries:
    """Serie de glucosa de una sola sesion de sensor, ordenada por tiempo.

    Invariante: ``ts`` estrictamente creciente y misma longitud que ``values``/``flags``.
    """

    ts: np.ndarray  # datetime64[s], UTC
    values: np.ndarray  # float64, mg/dL
    flags: np.ndarray  # int64, bitmask de QualityFlag
    session_id: str
    vendor: str

    def __post_init__(self) -> None:
        n = len(self.ts)
        if not (len(self.values) == len(self.flags) == n):
            raise ValueError("ts, values y flags deben tener la misma longitud")
        if n > 1 and not np.all(np.diff(self.ts.astype("datetime64[s]").astype(np.int64)) > 0):
            raise ValueError("ts debe ser estrictamente creciente")

    def __len__(self) -> int:
        return len(self.ts)

    @property
    def minutes(self) -> np.ndarray:
        """Tiempo en minutos desde la primera muestra, como float."""
        secs = self.ts.astype("datetime64[s]").astype(np.int64)
        return (secs - secs[0]) / 60.0


@dataclass(frozen=True, slots=True)
class WindowSeries:
    """Ventana postprandial: minutos RELATIVOS a la comida (negativos antes)."""

    rel_min: np.ndarray  # float64
    values: np.ndarray  # float64, mg/dL
    flags: np.ndarray  # int64
    resolution_min: int

    def slice(self, lo: float, hi: float) -> WindowSeries:
        m = (self.rel_min >= lo) & (self.rel_min <= hi)
        return WindowSeries(
            rel_min=self.rel_min[m],
            values=self.values[m],
            flags=self.flags[m],
            resolution_min=self.resolution_min,
        )

    @property
    def has_flagged(self) -> bool:
        blocking = (
            QualityFlag.OUT_OF_PHYSIOLOGICAL_RANGE.value
            | QualityFlag.IMPLAUSIBLE_RATE.value
            | QualityFlag.IN_SENSOR_WARMUP.value
            | QualityFlag.SUSPECTED_COMPRESSION_LOW.value
        )
        return bool(np.any(self.flags & blocking))

    def __len__(self) -> int:
        return len(self.rel_min)


@dataclass(frozen=True, slots=True)
class ResponseMetrics:
    """Metricas de una respuesta postprandial.

    Todo valor derivado viaja con su incertidumbre o su bandera de fiabilidad. Nunca se
    expone un escalar desnudo: es lo que obliga a la UI y al LLM a enfrentarse al ruido.
    """

    baseline_mgdl: float
    baseline_sd: float | None
    baseline_n: int

    peak_mgdl: float
    peak_delta_mgdl: float
    time_to_peak_min: float
    peak_underestimated: bool
    """True cuando la resolucion nativa > 5 min: el apex real cae entre muestras y el
    valor reportado es una COTA INFERIOR."""

    iauc_120: float | None
    iauc_180: float | None
    iauc_net_120: float | None
    auc_total_120: float | None

    time_above_baseline_min: float
    time_to_return_baseline_min: float | None
    cv_pct: float
    curve_shape: CurveShape

    coverage_pct: float
    max_gap_min: float
    n_points: int
    resolution_min: int


@dataclass(frozen=True, slots=True)
class PairingVerdict:
    """Resultado del emparejamiento comida <-> ventana glucemica.

    Una ventana excluida se CONSERVA con su razon: el ratio de exclusion es una metrica
    de producto, no un detalle de implementacion.
    """

    is_usable: bool
    exclusions: tuple[ExclusionReason, ...]
    degradations: tuple[DegradationReason, ...]
    prev_meal_gap_min: float | None
    next_meal_gap_min: float | None
    effective_post_window_min: int
    sensor_age_hours: float | None
