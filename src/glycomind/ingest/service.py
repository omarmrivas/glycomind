"""Persistencia de la ingesta CGM.

Idempotencia por diseno: la exportacion de LibreView es **manual y protegida por
reCAPTCHA**, asi que el usuario descargara rangos solapados una y otra vez. Reimportar
el mismo archivo, o uno que se solapa parcialmente, tiene que ser inocuo y tiene que
informar de que hay realmente de nuevo.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from glycomind.analysis.quality import assess_session, detect_session_step
from glycomind.config import AnalysisSettings, settings
from glycomind.db.models import (
    AppUser,
    CgmSensorSession,
    GlucoseReading,
    IngestBatch,
    Meal,
    MealItem,
)
from glycomind.domain.enums import Provenance
from glycomind.domain.models import ImportResult, RawFoodEntry, RawReading
from glycomind.ingest.libreview import parse_libreview_csv
from glycomind.ingest.sessions import DetectedSession, detect_sessions

_TAIL_POINTS = 12  # ~3 h a 15 min: muestra para medir el escalon entre sensores


def import_libreview_csv(
    db: Session,
    *,
    user: AppUser,
    data: bytes,
    filename: str | None = None,
    timezone: str | None = None,
    date_order_hint: str | None = None,
    cfg: AnalysisSettings | None = None,
) -> ImportResult:
    """Parsea y persiste un export de LibreView. Seguro de reejecutar."""
    cfg = cfg or settings.analysis
    tz = timezone or user.timezone
    parsed = parse_libreview_csv(data, timezone=tz, date_order_hint=date_order_hint)

    batch = IngestBatch(
        id=uuid.uuid4(),
        user_id=user.id,
        vendor="abbott",
        source="libreview_csv",
        source_filename=filename,
        content_sha256=parsed.content_sha256,
        is_official_api=False,
        # Base legal: el propio usuario descarga y aporta su archivo. Es la via oficial
        # de Abbott, no una API de ingenieria inversa.
        legal_basis="user_csv_upload",
        assumed_timezone=tz,
        detected_date_order=parsed.date_order,
        stats={},
        warnings=list(parsed.warnings),
    )
    db.add(batch)
    db.flush()

    warnings = list(parsed.warnings)
    if _already_imported(db, user.id, parsed.content_sha256, batch.id):
        warnings.append("Este archivo exacto ya se habia importado; no hay datos nuevos.")

    detected = detect_sessions(parsed.readings)
    session_ids, n_new_sessions = _upsert_sessions(db, user, detected)

    inserted, duplicates = _insert_readings(
        db,
        user=user,
        readings=parsed.readings,
        detected=detected,
        session_ids=session_ids,
        batch_id=batch.id,
        cfg=cfg,
    )
    food_inserted = _insert_food_entries(
        db, user=user, entries=parsed.food_entries, batch_id=batch.id
    )

    resolution = detected[-1].native_resolution_min if detected else None
    result = ImportResult(
        vendor="abbott",
        rows_parsed=parsed.rows_parsed,
        readings_found=len(parsed.readings),
        readings_inserted=inserted,
        readings_duplicate=duplicates,
        food_entries_found=len(parsed.food_entries),
        food_entries_inserted=food_inserted,
        sessions_created=n_new_sessions,
        first_ts_utc=parsed.readings[0].ts_utc if parsed.readings else None,
        last_ts_utc=parsed.readings[-1].ts_utc if parsed.readings else None,
        detected_resolution_min=resolution,
        warnings=warnings,
    )
    batch.stats = {
        "rows_parsed": result.rows_parsed,
        "readings_found": result.readings_found,
        "readings_inserted": result.readings_inserted,
        "readings_duplicate": result.readings_duplicate,
        "food_entries_inserted": result.food_entries_inserted,
        "sessions_created": result.sessions_created,
        "detected_resolution_min": resolution,
    }
    batch.warnings = warnings
    return result


def _already_imported(db: Session, user_id: uuid.UUID, sha: str, current: uuid.UUID) -> bool:
    stmt = select(IngestBatch.id).where(
        IngestBatch.user_id == user_id,
        IngestBatch.content_sha256 == sha,
        IngestBatch.id != current,
    )
    return db.execute(stmt).first() is not None


def _upsert_sessions(
    db: Session, user: AppUser, detected: Sequence[DetectedSession]
) -> tuple[dict[str, uuid.UUID], int]:
    existing = {
        s.device_serial_hash: s
        for s in db.execute(
            select(CgmSensorSession).where(CgmSensorSession.user_id == user.id)
        ).scalars()
    }
    ids: dict[str, uuid.UUID] = {}
    created = 0
    for d in detected:
        row = existing.get(d.device_serial_hash)
        if row is None:
            row = CgmSensorSession(
                id=uuid.uuid4(),
                user_id=user.id,
                vendor="abbott",
                model=None,
                device_serial_hash=d.device_serial_hash,
                started_at=d.started_at,
                ended_at=d.ended_at,
                native_resolution_min=d.native_resolution_min,
                n_readings=d.n_readings,
            )
            db.add(row)
            created += 1
        else:
            # Una importacion posterior puede ampliar el rango por ambos extremos.
            row.started_at = min(row.started_at, _aware(d.started_at))
            row.ended_at = max(row.ended_at or _aware(d.ended_at), _aware(d.ended_at))
            row.native_resolution_min = d.native_resolution_min
            row.n_readings = max(row.n_readings, d.n_readings)
        db.flush()
        ids[d.device_serial_hash] = row.id
    return ids, created


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _insert_readings(
    db: Session,
    *,
    user: AppUser,
    readings: Sequence[RawReading],
    detected: Sequence[DetectedSession],
    session_ids: dict[str, uuid.UUID],
    batch_id: uuid.UUID,
    cfg: AnalysisSettings,
) -> tuple[int, int]:
    if not readings:
        return 0, 0

    rows: list[dict] = []
    prev_tail: np.ndarray | None = None

    for d in detected:
        items = [
            r
            for r in readings
            if r.device_serial_hash == d.device_serial_hash and r.source_record == "historic"
        ]
        items.sort(key=lambda r: r.ts_utc)
        ts = np.array([np.datetime64(r.ts_utc.replace(tzinfo=None), "s") for r in items])
        vals = np.array([r.value_mgdl for r in items], dtype=float)

        report = assess_session(
            ts,
            vals,
            session_start=np.datetime64(d.started_at.replace(tzinfo=None), "s"),
            resolution_min=d.native_resolution_min,
            cfg=cfg,
        )
        flags = report.flags

        step = detect_session_step(
            prev_tail if prev_tail is not None else np.array([]), vals[:_TAIL_POINTS], cfg
        )
        if step is not None:
            row = db.get(CgmSensorSession, session_ids[d.device_serial_hash])
            if row is not None:
                row.step_vs_previous_mgdl = step
        prev_tail = vals[-_TAIL_POINTS:]

        for r, f in zip(items, flags, strict=True):
            rows.append(
                {
                    "user_id": user.id,
                    "ts_utc": r.ts_utc,
                    "session_id": session_ids[d.device_serial_hash],
                    "tz_offset_min": r.tz_offset_min,
                    "value_mgdl": r.value_mgdl,
                    "source_record": r.source_record,
                    "quality_flags": int(f),
                    "ingest_batch_id": batch_id,
                }
            )

    # Los escaneos y tiras se guardan pero no participan en el analisis continuo.
    for r in readings:
        if r.source_record == "historic":
            continue
        sid = session_ids.get(r.device_serial_hash)
        if sid is None:
            continue
        rows.append(
            {
                "user_id": user.id,
                "ts_utc": r.ts_utc,
                "session_id": sid,
                "tz_offset_min": r.tz_offset_min,
                "value_mgdl": r.value_mgdl,
                "source_record": r.source_record,
                "quality_flags": 0,
                "ingest_batch_id": batch_id,
            }
        )

    if not rows:
        return 0, 0

    # Deduplicar dentro del propio lote antes de tocar la base: un mismo instante puede
    # traer historico y escaneo, y ON CONFLICT no protege de duplicados intra-sentencia.
    seen: set[tuple] = set()
    unique: list[dict] = []
    for row in rows:
        key = (row["user_id"], row["ts_utc"], row["session_id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    # Se cuenta con RETURNING, no con ``rowcount``. Con listas grandes SQLAlchemy usa
    # "insertmanyvalues" y parte la sentencia en varios lotes, con lo que ``rowcount``
    # deja de ser fiable: reportaba 0 insertadas habiendo insertado 1305. Un contador
    # que miente sobre una ingesta idempotente destruye la confianza en todo el pipeline.
    # Con ON CONFLICT DO NOTHING, RETURNING devuelve SOLO las filas realmente insertadas.
    stmt = (
        pg_insert(GlucoseReading)
        .values(unique)
        .on_conflict_do_nothing(index_elements=["user_id", "ts_utc", "session_id"])
        .returning(GlucoseReading.ts_utc)
    )
    inserted = len(db.execute(stmt).all())
    return inserted, len(unique) - inserted


def _insert_food_entries(
    db: Session, *, user: AppUser, entries: Sequence[RawFoodEntry], batch_id: uuid.UUID
) -> int:
    """Convierte los registros de comida de la app de Abbott en ``Meal``.

    Glooko descarta estos datos; nosotros no. Son un registro de comidas con friccion
    cero (sin foto ni descripcion rica, pero con hora fiable), y la hora es justamente
    lo que mas importa para emparejar la ventana glucemica.
    """
    if not entries:
        return 0
    existing = {
        ts
        for (ts,) in db.execute(
            select(Meal.consumed_at).where(Meal.user_id == user.id, Meal.source == "libreview_food")
        )
    }
    created = 0
    for e in entries:
        if _aware(e.ts_utc) in existing:
            continue
        meal = Meal(
            id=uuid.uuid4(),
            user_id=user.id,
            consumed_at=e.ts_utc,
            tz_offset_min=e.tz_offset_min,
            free_text=e.note,
            source="libreview_food",
            ingest_batch_id=batch_id,
            # Sin foto ni items desglosados: la completitud es baja y el modelo de la
            # Fase 2 debe ponderarla en consecuencia.
            entry_completeness=0.3 if e.carbs_grams is not None else 0.15,
        )
        db.add(meal)
        if e.carbs_grams is not None:
            db.add(
                MealItem(
                    id=uuid.uuid4(),
                    meal_id=meal.id,
                    raw_label=e.note or "carbohidratos registrados en la app",
                    carbs_g=e.carbs_grams,
                    quantity_value=e.carbs_grams,
                    quantity_unit="g_carb",
                    provenance=Provenance.USER.value,
                )
            )
        created += 1
    return created
