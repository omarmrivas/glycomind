"""Configuracion global.

Los umbrales de analisis viven aqui y NO se hardcodean en los algoritmos, porque son
decisiones cientificas que hay que poder cambiar y versionar. Cambiar cualquiera de estos
valores obliga a subir ``ALGORITHM_VERSION`` y recalcular.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Version del conjunto {metricas + reglas de QC + reglas de pairing}.
# Se persiste en cada meal_glucose_response. Sin esto el historico es inauditable.
ALGORITHM_VERSION = "metrics@1.0.0"


class AnalysisSettings(BaseSettings):
    """Umbrales del nucleo cuantitativo.

    Casi todos son *funciones de la resolucion nativa detectada*, no constantes: el
    FreeStyle Libre 2 Plus exporta a 15 min y Dexcom a 5 min, y una regla fija romperia
    uno de los dos. Ver docs/07-cgm.md seccion 1.1.
    """

    model_config = SettingsConfigDict(env_prefix="GLYCOMIND_ANALYSIS_")

    # --- ventana postprandial ---
    post_window_min: int = 180
    baseline_offset_min: int = 5
    """La basal se mide hasta 5 min antes de la comida (evita el primer bocado)."""

    # --- calidad de senal ---
    min_coverage_pct: float = 85.0
    physiological_min_mgdl: float = 40.0
    physiological_max_mgdl: float = 400.0
    max_rate_mgdl_per_min: float = 6.0
    analysis_warmup_hours: float = 12.0
    """Distinto del warm-up del fabricante (~1 h): responde al error elevado documentado
    en las primeras horas de vida del sensor."""

    # --- contaminacion por comidas vecinas ---
    exclude_if_prev_meal_within_min: int = 180
    exclude_if_next_meal_within_min: int = 120
    degrade_if_next_meal_within_min: int = 180

    # --- otros ---
    exclude_if_vendor_changed_within_h: float = 24.0
    sensor_step_threshold_mgdl: float = 15.0
    return_to_baseline_margin_mgdl: float = 10.0
    flat_curve_threshold_mgdl: float = 15.0
    peak_reliable_max_resolution_min: int = 5
    """Por encima de esta resolucion, el pico se reporta como cota inferior."""

    # --- deteccion de compression lows ---
    compression_drop_mgdl: float = 30.0
    compression_drop_window_min: int = 30
    compression_recovery_min: int = 60

    def pre_window_min(self, resolution_min: int) -> int:
        """Cuanto mirar hacia atras. Necesita caber la ventana de basal completa."""
        return max(30, 2 * resolution_min + self.baseline_offset_min + 5)

    def baseline_window_min(self, resolution_min: int) -> int:
        """Ancho de la ventana de basal. Con 15 min de resolucion, 30 min da 2-3 puntos."""
        return max(20, 2 * resolution_min)

    def max_gap_min(self, resolution_min: int) -> float:
        """Hueco maximo tolerado. Con 15 min => 37.5, o sea >2 muestras consecutivas perdidas."""
        return max(20.0, 2.5 * resolution_min)

    def min_sustained_min(self, resolution_min: int) -> int:
        """Duracion minima para considerar una condicion 'sostenida'."""
        return max(15, resolution_min)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GLYCOMIND_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://glycomind:glycomind@localhost:5432/glycomind"
    default_timezone: str = "America/Mexico_City"
    sql_echo: bool = False
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)


settings = Settings()
