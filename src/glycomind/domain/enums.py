"""Enumeraciones del dominio."""

from __future__ import annotations

from enum import Flag, StrEnum, auto


class QualityFlag(Flag):
    """Banderas por lectura de glucosa. Bitmask: se MARCA, nunca se borra.

    La politica de exclusion vive en la consulta, no en la ingesta, para poder cambiarla
    sin reimportar datos.
    """

    NONE = 0
    OUT_OF_PHYSIOLOGICAL_RANGE = auto()
    IMPLAUSIBLE_RATE = auto()
    IN_SENSOR_WARMUP = auto()
    SUSPECTED_COMPRESSION_LOW = auto()
    SENSOR_TRANSITION_STEP = auto()
    SCAN_VALUE = auto()
    """Lectura de escaneo (Record Type 1), no del historico continuo."""

    @property
    def excludes_from_analysis(self) -> bool:
        blocking = (
            QualityFlag.OUT_OF_PHYSIOLOGICAL_RANGE
            | QualityFlag.IMPLAUSIBLE_RATE
            | QualityFlag.IN_SENSOR_WARMUP
            | QualityFlag.SUSPECTED_COMPRESSION_LOW
        )
        return bool(self & blocking)


class ResponseQuality(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    EXCLUDED = "excluded"


class ExclusionReason(StrEnum):
    NO_GLUCOSE_DATA = "no_glucose_data"
    NO_BASELINE = "no_baseline"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    GAP_TOO_LARGE = "gap_too_large"
    SENSOR_WARMUP = "sensor_warmup"
    PREV_MEAL_TOO_CLOSE = "prev_meal_too_close"
    NEXT_MEAL_TOO_SOON = "next_meal_too_soon"
    SENSOR_SESSION_CHANGE = "sensor_session_change"
    VENDOR_CHANGE = "vendor_change"
    FLAGGED_READINGS = "flagged_readings"


class DegradationReason(StrEnum):
    NEXT_MEAL_LIMITS_WINDOW = "next_meal_limits_window"
    PEAK_UNDERESTIMATED = "peak_underestimated"
    WEAK_BASELINE = "weak_baseline"
    ACTIVITY_IN_WINDOW = "activity_in_window"
    PARTIAL_COVERAGE = "partial_coverage"


class CurveShape(StrEnum):
    FLAT = "flat"
    MONOPHASIC = "monophasic"
    BIPHASIC = "biphasic"
    PLATEAU = "plateau"
    UNKNOWN = "unknown"


class Provenance(StrEnum):
    """De donde viene cada dato. Determina su peso en el modelo estadistico (Fase 2)."""

    OBSERVED = "observed"
    INFERRED = "inferred"
    USER = "user_reported"
    DATABASE = "database"
    DEFAULT = "assumed_default"


class MealType(StrEnum):
    DESAYUNO = "desayuno"
    COMIDA = "comida"
    CENA = "cena"
    COLACION = "colacion"
    UNKNOWN = "unknown"
