"""Tests del parser de LibreView.

Los casos cubren fallos reales documentados, no hipoteticos: ambiguedad de orden de
fecha (el articulo de soporte de Abbott en espanol usa un ejemplo mes-primero),
re-guardado en Excel (Abbott advierte explicitamente que rompe la importacion),
localizacion de cabeceras y unidades mmol/L.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from glycomind.ingest.libreview import (
    LibreViewParseError,
    detect_date_order,
    iter_historic,
    parse_libreview_csv,
)
from tests.factories import HEADERS_ES, build_libreview_csv, fmt_ts, synth_series

TZ = "America/Mexico_City"


def two_weeks(resolution_min: int = 15):
    return synth_series(
        datetime(2026, 8, 1, 0, 0),
        hours=24 * 14,
        resolution_min=resolution_min,
        meals=[(datetime(2026, 8, 1, 13, 20), 60.0, 75.0)],
    )


# --------------------------------------------------------------------------------------
# Orden de fecha
# --------------------------------------------------------------------------------------


def test_detects_dmy_from_day_over_twelve():
    stamps = ["01-08-2026 07:00", "14-08-2026 07:15", "28-08-2026 07:30"]
    order, _ = detect_date_order(stamps)
    assert order == "DMY"


def test_detects_mdy_from_day_over_twelve():
    stamps = ["08-01-2026 07:00", "08-14-2026 07:15", "08-28-2026 07:30"]
    order, _ = detect_date_order(stamps)
    assert order == "MDY"


def test_detects_iso_year_first():
    stamps = ["2026-08-01 07:00", "2026-08-02 07:15"]
    order, _ = detect_date_order(stamps)
    assert order == "YMD"


def test_contradictory_date_formats_are_rejected():
    """Dias >12 en ambas posiciones: el archivo fue manipulado."""
    stamps = ["14-08-2026 07:00", "08-28-2026 07:15"]
    with pytest.raises(LibreViewParseError, match="mezcla formatos"):
        detect_date_order(stamps)


def test_ambiguous_multiday_resolved_by_temporal_density():
    """Ningun dia >12, pero interpretar 5 dias como 5 meses hunde la densidad."""
    readings = synth_series(datetime(2026, 8, 1), hours=24 * 5, resolution_min=15)
    csv = build_libreview_csv(readings=readings, date_order="DMY")
    parsed = parse_libreview_csv(csv, timezone=TZ)
    assert parsed.date_order == "DMY"
    assert any("densidad temporal" in w for w in parsed.warnings)


def test_single_day_export_refuses_to_guess():
    """Un solo dia es genuinamente ambiguo: fallar es mejor que desplazar 7 meses."""
    readings = synth_series(datetime(2026, 8, 1), hours=8, resolution_min=15)
    csv = build_libreview_csv(readings=readings, date_order="DMY")
    with pytest.raises(LibreViewParseError, match="DD-MM-AAAA o MM-DD-AAAA"):
        parse_libreview_csv(csv, timezone=TZ)


def test_explicit_hint_resolves_ambiguity():
    readings = synth_series(datetime(2026, 8, 1), hours=8, resolution_min=15)
    csv = build_libreview_csv(readings=readings, date_order="DMY")
    parsed = parse_libreview_csv(csv, timezone=TZ, date_order_hint="DMY")
    assert parsed.date_order == "DMY"
    assert parsed.readings[0].ts_utc.month == 8


def test_slash_separator_and_seconds_are_accepted():
    stamps = [fmt_ts(datetime(2026, 8, d, 7, 0), "DMY", sep="/") for d in (1, 14, 28)]
    order, _ = detect_date_order(stamps)
    assert order == "DMY"


# --------------------------------------------------------------------------------------
# Zona horaria
# --------------------------------------------------------------------------------------


def test_local_wall_clock_converted_to_utc():
    """El CSV no lleva zona: hay que pasarla y convertir explicitamente."""
    csv = build_libreview_csv(readings=two_weeks(), date_order="DMY")
    parsed = parse_libreview_csv(csv, timezone=TZ)
    first = parsed.readings[0]
    # Medianoche local en Ciudad de Mexico (UTC-6) = 06:00 UTC.
    assert first.ts_utc.hour == 6
    assert first.tz_offset_min == -360


def test_timezone_changes_result():
    csv = build_libreview_csv(readings=two_weeks(), date_order="DMY")
    mx = parse_libreview_csv(csv, timezone=TZ).readings[0]
    utc = parse_libreview_csv(csv, timezone="UTC").readings[0]
    assert mx.ts_utc != utc.ts_utc


# --------------------------------------------------------------------------------------
# Localizacion y robustez de formato
# --------------------------------------------------------------------------------------


def test_spanish_headers_are_recognized():
    csv = build_libreview_csv(readings=two_weeks(), date_order="DMY", headers=HEADERS_ES)
    parsed = parse_libreview_csv(csv, timezone=TZ)
    assert len(parsed.readings) > 1000


def test_semicolon_delimiter_warns_about_excel():
    """Abbott advierte que re-guardar el archivo rompe el formato. Lo detectamos."""
    csv = build_libreview_csv(readings=two_weeks(), date_order="DMY", delimiter=";")
    parsed = parse_libreview_csv(csv, timezone=TZ)
    assert any("Excel" in w for w in parsed.warnings)
    assert len(parsed.readings) > 1000


def test_mmol_is_converted_to_mgdl():
    readings = [(t, round(v / 18.0182, 1)) for t, v in two_weeks()]
    csv = build_libreview_csv(readings=readings, date_order="DMY", unit_label="mmol/L")
    parsed = parse_libreview_csv(csv, timezone=TZ)
    assert parsed.glucose_unit == "mmol/L"
    assert 90.0 < parsed.readings[0].value_mgdl < 100.0


def test_two_preamble_lines_are_skipped():
    csv = build_libreview_csv(readings=two_weeks(), date_order="DMY", preamble_lines=2)
    parsed = parse_libreview_csv(csv, timezone=TZ)
    assert len(parsed.readings) > 1000


def test_missing_glucose_columns_fails_clearly():
    csv = (
        b"Glucose Data,Generated on\n"
        b"Device,Serial Number,Device Timestamp,Record Type,Notes\n"
        b"X,Y,01-08-2026 07:00,5,hola\n"
    )
    with pytest.raises(LibreViewParseError, match="columnas de glucosa"):
        parse_libreview_csv(csv, timezone=TZ)


def test_not_a_libreview_file_fails_clearly():
    with pytest.raises(LibreViewParseError, match="cabecera"):
        parse_libreview_csv(b"a,b,c\n1,2,3\n", timezone=TZ)


# --------------------------------------------------------------------------------------
# Tipos de registro
# --------------------------------------------------------------------------------------


def test_scans_are_kept_but_separated_from_historic():
    """Los escaneos caen en instantes arbitrarios: incluirlos corromperia la resolucion."""
    scans = [(datetime(2026, 8, 3, 9, 7), 118.0), (datetime(2026, 8, 4, 20, 41), 143.0)]
    csv = build_libreview_csv(readings=two_weeks(), date_order="DMY", scans=scans)
    parsed = parse_libreview_csv(csv, timezone=TZ)
    historic = list(iter_historic(parsed.readings))
    assert len(parsed.readings) - len(historic) == 2
    assert all(r.source_record == "historic" for r in historic)


def test_food_entries_are_extracted():
    """Glooko descarta carbohidratos y notas; nosotros los usamos como registro de comida."""
    foods = [
        (datetime(2026, 8, 2, 13, 20), 45.0, "tortillas, frijoles, pollo, aguacate"),
        (datetime(2026, 8, 3, 8, 5), None, "cafe sin azucar"),
    ]
    csv = build_libreview_csv(readings=two_weeks(), date_order="DMY", foods=foods)
    parsed = parse_libreview_csv(csv, timezone=TZ)
    assert len(parsed.food_entries) == 2
    assert parsed.food_entries[0].carbs_grams == pytest.approx(45.0)
    assert parsed.food_entries[1].carbs_grams is None
    assert "cafe" in (parsed.food_entries[1].note or "")


def test_content_hash_is_stable():
    csv = build_libreview_csv(readings=two_weeks(), date_order="DMY")
    a = parse_libreview_csv(csv, timezone=TZ)
    b = parse_libreview_csv(csv, timezone=TZ)
    assert a.content_sha256 == b.content_sha256
