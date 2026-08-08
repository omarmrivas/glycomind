"""Integracion contra PostgreSQL real: ingesta -> sesiones -> pairing -> metricas.

Requiere la base levantada (``docker compose up -d postgres`` + ``alembic upgrade head``).
Se omiten si no hay conexion, para que la suite unitaria siga corriendo sin infra.

    uv run pytest -m db
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select, text

from glycomind.analysis.pipeline import analyze_user
from glycomind.db.models import AppUser, Meal
from glycomind.db.session import SessionLocal, engine
from glycomind.ingest.libreview import LibreViewParseError
from glycomind.ingest.service import import_libreview_csv
from tests.factories import build_libreview_csv, synth_series

pytestmark = pytest.mark.db

TZ = "America/Mexico_City"
START = datetime(2026, 3, 2, 0, 0)


@pytest.fixture(scope="module", autouse=True)
def _require_db():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - depende del entorno
        pytest.skip(f"PostgreSQL no disponible: {exc}")


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture
def user(db):
    u = AppUser(
        id=uuid.uuid4(),
        email=f"test-{uuid.uuid4().hex[:12]}@example.invalid",
        timezone=TZ,
    )
    db.add(u)
    db.commit()
    yield u
    db.execute(delete(AppUser).where(AppUser.id == u.id))
    db.commit()


def make_csv(*, serial: str = "TESTSENSOR1", days: int = 14, meal_amp: float = 60.0) -> bytes:
    meals = [(START + timedelta(days=d, hours=13, minutes=20), meal_amp, 75.0) for d in range(days)]
    readings = synth_series(START, hours=24 * days, resolution_min=15, meals=meals)
    return build_libreview_csv(readings=readings, serial=serial, date_order="DMY")


def test_import_then_reimport_is_idempotent(db, user):
    """Regresion: ``rowcount`` mentia con insertmanyvalues y reportaba 0 insertadas."""
    csv = make_csv(days=14)

    first = import_libreview_csv(db, user=user, data=csv, filename="a.csv")
    db.commit()
    assert first.readings_inserted > 1000
    assert first.readings_duplicate == 0
    assert first.sessions_created == 1
    assert first.detected_resolution_min == 15

    second = import_libreview_csv(db, user=user, data=csv, filename="a.csv")
    db.commit()
    assert second.readings_inserted == 0
    assert second.readings_duplicate == first.readings_inserted
    assert any("ya se habia importado" in w for w in second.warnings)


def test_overlapping_import_only_adds_the_new_tail(db, user):
    """El usuario descarga rangos solapados: es el caso normal, no el excepcional."""
    import_libreview_csv(db, user=user, data=make_csv(days=7), filename="semana1.csv")
    db.commit()

    extended = import_libreview_csv(db, user=user, data=make_csv(days=14), filename="dos.csv")
    db.commit()
    assert extended.readings_duplicate > 600  # la primera semana ya estaba
    assert extended.readings_inserted > 600  # la segunda es nueva
    assert extended.sessions_created == 0  # mismo sensor


def test_second_sensor_creates_second_session(db, user):
    import_libreview_csv(db, user=user, data=make_csv(serial="S1"), filename="s1.csv")
    db.commit()
    result = import_libreview_csv(db, user=user, data=make_csv(serial="S2"), filename="s2.csv")
    db.commit()
    assert result.sessions_created == 1


def test_full_pipeline_produces_usable_responses(db, user):
    import_libreview_csv(db, user=user, data=make_csv(days=14), filename="a.csv")
    db.commit()

    # Una comida por dia a las 13:20 local, alineada con los picos sinteticos.
    for d in range(14):
        _add_meal(db, user, START + timedelta(days=d, hours=13, minutes=20))
    db.commit()

    summary = analyze_user(db, user_id=user.id)
    db.commit()

    assert summary.meals_total == 14
    assert summary.usable == 14
    assert summary.pairing_valid_ratio == 1.0

    rerun = analyze_user(db, user_id=user.id)
    assert rerun.computed == 0
    assert rerun.skipped_existing == 14


def test_meal_during_sensor_warmup_is_excluded(db, user):
    """Las primeras 12 h de un sensor se descartan por el error elevado documentado.

    Es distinto del warm-up del fabricante (~1 h) y cuesta ~3.3% de los datos en un
    sensor de 15 dias. Es un coste que vale la pena.
    """
    import_libreview_csv(db, user=user, data=make_csv(days=14), filename="a.csv")
    db.commit()

    _add_meal(db, user, START + timedelta(hours=3))  # dentro del warm-up
    _add_meal(db, user, START + timedelta(days=5, hours=13, minutes=20))  # fuera
    db.commit()

    summary = analyze_user(db, user_id=user.id)
    assert summary.excluded == 1
    assert summary.usable == 1
    assert summary.exclusion_counts.get("sensor_warmup") == 1


def _add_meal(db, user, naive_local: datetime, text: str = "comida de prueba") -> None:
    local = naive_local.replace(tzinfo=ZoneInfo(TZ))
    offset = local.utcoffset()
    db.add(
        Meal(
            id=uuid.uuid4(),
            user_id=user.id,
            consumed_at=local,
            tz_offset_min=int(offset.total_seconds() // 60) if offset else 0,
            free_text=text,
            source="test",
        )
    )


def test_food_entries_from_app_become_meals(db, user):
    """Glooko descarta carbohidratos y notas; nosotros los usamos como registro."""
    readings = synth_series(START, hours=24 * 14, resolution_min=15)
    foods = [
        (START + timedelta(days=2, hours=13, minutes=20), 45.0, "tacos"),
        (START + timedelta(days=3, hours=8, minutes=5), None, "cafe"),
    ]
    csv = build_libreview_csv(readings=readings, date_order="DMY", foods=foods)

    result = import_libreview_csv(db, user=user, data=csv, filename="con-comidas.csv")
    db.commit()
    assert result.food_entries_inserted == 2

    meals = db.execute(
        select(Meal).where(Meal.user_id == user.id, Meal.source == "libreview_food")
    ).scalars()
    labels = {m.free_text for m in meals}
    assert labels == {"tacos", "cafe"}

    # Reimportar no duplica comidas.
    again = import_libreview_csv(db, user=user, data=csv, filename="con-comidas.csv")
    db.commit()
    assert again.food_entries_inserted == 0


def test_ambiguous_single_day_export_is_rejected(db, user):
    readings = synth_series(START, hours=8, resolution_min=15)
    csv = build_libreview_csv(readings=readings, date_order="DMY")
    with pytest.raises(LibreViewParseError):
        import_libreview_csv(db, user=user, data=csv, filename="un-dia.csv")
