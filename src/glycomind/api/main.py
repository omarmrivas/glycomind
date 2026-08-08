"""API de lectura de la Fase 1.

Regla de diseno que se mantiene desde el principio: **ningun endpoint devuelve un
escalar desnudo**. Toda cifra derivada viaja con su n, su calidad, su incertidumbre y la
version de algoritmo que la produjo. Es lo que obliga a la UI --y mas adelante al
LLM-- a enfrentarse al ruido en vez de esconderlo.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from glycomind.analysis.pipeline import analyze_user
from glycomind.config import ALGORITHM_VERSION
from glycomind.db.models import AppUser, Meal, MealGlucoseResponse
from glycomind.db.session import SessionLocal
from glycomind.ingest.libreview import LibreViewParseError
from glycomind.ingest.service import import_libreview_csv

app = FastAPI(
    title="GlycoMind API",
    version="0.1.0",
    description=(
        "Fase 1: pipeline determinista comida <-> respuesta glucemica. "
        "Herramienta de bienestar general; no es un dispositivo medico."
    ),
)


def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


DB = Annotated[Session, Depends(get_db)]


def _user(db: Session, email: str) -> AppUser:
    user = db.execute(select(AppUser).where(AppUser.email == email)).scalar_one_or_none()
    if user is None:
        raise HTTPException(404, f"usuario no encontrado: {email}")
    return user


class MealResponseOut(BaseModel):
    meal_id: uuid.UUID
    consumed_at: datetime
    free_text: str | None
    quality: Literal["ok", "degraded", "excluded"]
    exclusion_reasons: list[str]
    degradation_reasons: list[str]
    baseline_mgdl: float | None
    peak_delta_mgdl: float | None
    peak_is_lower_bound: bool | None = Field(
        None,
        description=(
            "Si es true, el pico es una COTA INFERIOR: a resolucion gruesa el maximo "
            "real cae entre muestras."
        ),
    )
    time_to_peak_min: float | None
    time_to_peak_precision_min: int | None = Field(
        None, description="Granularidad del tiempo al pico, igual a la resolucion nativa."
    )
    iauc_120: float | None
    iauc_180: float | None
    curve_shape: str | None
    coverage_pct: float | None
    resolution_min: int | None
    algorithm_version: str


class FoodSummaryOut(BaseModel):
    food_label: str
    n_exposures: int
    iauc_120_median: float | None
    iauc_120_q1: float | None
    iauc_120_q3: float | None
    iauc_120_min: float | None
    iauc_120_max: float | None
    peak_delta_median: float | None
    evidence_status: str
    interpretation: str


class AnalysisOut(BaseModel):
    meals_total: int
    computed: int
    usable: int
    degraded: int
    excluded: int
    pairing_valid_ratio: float
    exclusion_counts: dict[str, int]
    note: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "algorithm_version": ALGORITHM_VERSION}


@app.post("/v1/cgm/import", summary="Importa un CSV de LibreView")
async def cgm_import(
    db: DB,
    file: UploadFile,
    email: Annotated[str, Query(description="Correo del usuario.")],
    timezone: Annotated[str | None, Query()] = None,
    date_order: Annotated[str | None, Query(description="DMY|MDY|YMD si es ambiguo.")] = None,
) -> dict:
    """La exportacion de LibreView es manual (reCAPTCHA); no se puede automatizar.

    Reimportar archivos solapados es seguro: la ingesta es idempotente.
    """
    user = _user(db, email)
    data = await file.read()
    try:
        result = import_libreview_csv(
            db,
            user=user,
            data=data,
            filename=file.filename,
            timezone=timezone,
            date_order_hint=date_order,
        )
    except LibreViewParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "readings_found": result.readings_found,
        "readings_inserted": result.readings_inserted,
        "readings_duplicate": result.readings_duplicate,
        "sessions_created": result.sessions_created,
        "food_entries_inserted": result.food_entries_inserted,
        "detected_resolution_min": result.detected_resolution_min,
        "range_utc": [result.first_ts_utc, result.last_ts_utc],
        "warnings": result.warnings,
    }


@app.post("/v1/analysis/run", response_model=AnalysisOut)
def run_analysis(db: DB, email: Annotated[str, Query()], recompute: bool = False) -> AnalysisOut:
    user = _user(db, email)
    s = analyze_user(db, user_id=user.id, recompute=recompute)
    return AnalysisOut(
        meals_total=s.meals_total,
        computed=s.computed,
        usable=s.usable,
        degraded=s.degraded,
        excluded=s.excluded,
        pairing_valid_ratio=s.pairing_valid_ratio,
        exclusion_counts=s.exclusion_counts,
        note=(
            "pairing_valid_ratio por debajo de 0.6 indica un problema de captura de "
            "comidas o de adherencia al sensor, no de estadistica."
        ),
    )


@app.get("/v1/meals/responses", response_model=list[MealResponseOut])
def list_responses(
    db: DB,
    email: Annotated[str, Query()],
    limit: Annotated[int, Query(le=500)] = 50,
    quality: Annotated[list[str] | None, Query()] = None,
) -> list[MealResponseOut]:
    user = _user(db, email)
    stmt = (
        select(MealGlucoseResponse, Meal)
        .join(Meal, Meal.id == MealGlucoseResponse.meal_id)
        .where(MealGlucoseResponse.user_id == user.id)
        .order_by(Meal.consumed_at.desc())
        .limit(limit)
    )
    if quality:
        stmt = stmt.where(MealGlucoseResponse.quality.in_(quality))
    return [
        MealResponseOut(
            meal_id=r.meal_id,
            consumed_at=m.consumed_at,
            free_text=m.free_text,
            quality=r.quality,
            exclusion_reasons=r.exclusion_reasons,
            degradation_reasons=r.degradation_reasons,
            baseline_mgdl=r.baseline_mgdl,
            peak_delta_mgdl=r.peak_delta_mgdl,
            peak_is_lower_bound=r.peak_underestimated,
            time_to_peak_min=r.time_to_peak_min,
            time_to_peak_precision_min=r.resolution_min,
            iauc_120=r.iauc_120,
            iauc_180=r.iauc_180,
            curve_shape=r.curve_shape,
            coverage_pct=r.coverage_pct,
            resolution_min=r.resolution_min,
            algorithm_version=r.algorithm_version,
        )
        for r, m in db.execute(stmt).all()
    ]


_INTERPRETATION = {
    "suficiente_para_observacion": (
        "Hay suficientes exposiciones para describir una tendencia personal, pero sigue "
        "siendo observacional: la hora, la actividad y la comida previa no estan "
        "controladas. Mira el rango, no solo la mediana."
    ),
    "insuficiente_tendencia_no_concluyente": (
        "Demasiadas pocas exposiciones para concluir nada. La variabilidad de la "
        "respuesta a una misma comida es alta (ICC intraindividual 0.14-0.31)."
    ),
    "insuficiente_no_interpretar": (
        "No interpretar. Con este numero de exposiciones, la diferencia observada es "
        "indistinguible del ruido de medicion y biologico."
    ),
}


@app.get("/v1/insights/foods", response_model=list[FoodSummaryOut])
def food_summary(db: DB, email: Annotated[str, Query()]) -> list[FoodSummaryOut]:
    """Resumen por alimento, con el estado de evidencia SIEMPRE explicito.

    Nunca devuelve una media desnuda: por debajo de 8 exposiciones validas no hay
    hallazgo que reportar, solo observaciones sueltas.
    """
    user = _user(db, email)
    rows = db.execute(
        text("SELECT * FROM v_food_summary WHERE user_id = :uid ORDER BY n_exposures DESC"),
        {"uid": str(user.id)},
    ).mappings()
    return [
        FoodSummaryOut(
            **{k: v for k, v in row.items() if k != "user_id"},
            interpretation=_INTERPRETATION[row["evidence_status"]],
        )
        for row in rows
    ]
