"""Tests de oraculo del Metrics Engine.

Cada caso tiene un valor calculable a mano. Si estos tests pasan, el numero que ve el
usuario es el numero correcto; si no, todo lo que se construya encima es ruido con
formato bonito.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from glycomind.analysis.metrics import (
    _positive_area_and_time,
    compute_baseline,
    compute_metrics,
    detect_resolution_min,
)
from glycomind.config import AnalysisSettings
from glycomind.domain.enums import CurveShape
from glycomind.domain.models import WindowSeries

CFG = AnalysisSettings()


def make_window(points: list[tuple[float, float]], resolution_min: int = 15) -> WindowSeries:
    rel = np.array([p[0] for p in points], dtype=float)
    vals = np.array([p[1] for p in points], dtype=float)
    return WindowSeries(
        rel_min=rel,
        values=vals,
        flags=np.zeros(len(rel), dtype=np.int64),
        resolution_min=resolution_min,
    )


def triangular(baseline: float, amplitude: float, ttp: float, width: float, res: int) -> list:
    """Rampa lineal hasta el pico y bajada lineal simetrica, muestreada cada ``res``."""
    pts = [(t, baseline) for t in np.arange(-35.0, 0.0, res)]
    for t in np.arange(0.0, width + res, res):
        if t <= ttp:
            v = baseline + amplitude * (t / ttp)
        elif t <= width:
            v = baseline + amplitude * (1 - (t - ttp) / (width - ttp))
        else:
            v = baseline
        pts.append((float(t), float(v)))
    return pts


# --------------------------------------------------------------------------------------
# Area con cruce de basal: el caso que distingue una implementacion correcta
# --------------------------------------------------------------------------------------


def test_positive_area_splits_at_baseline_crossing():
    """d cruza cero a mitad de segmento: hay que partir en el cruce exacto.

    Truncar punto a punto (max(d,0) y luego trapecio) daria 600 en vez de 450:
    un 33% de sobreestimacion sistematica del iAUC.
    """
    t = np.array([0.0, 30.0, 60.0])
    d = np.array([0.0, 20.0, -20.0])
    area, time_above = _positive_area_and_time(t, d)

    assert area == pytest.approx(450.0)
    assert time_above == pytest.approx(45.0)

    naive = float(np.trapezoid(np.maximum(d, 0.0), t))
    assert naive == pytest.approx(600.0)
    assert area < naive


def test_positive_area_all_below_baseline_is_zero():
    t = np.array([0.0, 15.0, 30.0])
    d = np.array([-5.0, -10.0, -2.0])
    area, time_above = _positive_area_and_time(t, d)
    assert area == 0.0
    assert time_above == 0.0


def test_positive_area_upward_crossing():
    """d1 < 0 < d2: solo cuenta el tramo posterior al cruce."""
    t = np.array([0.0, 40.0])
    d = np.array([-10.0, 30.0])
    area, time_above = _positive_area_and_time(t, d)
    # cruce en frac = 30/40 desde el final -> tramo positivo de 30 min, triangulo h=30
    assert time_above == pytest.approx(30.0)
    assert area == pytest.approx(0.5 * 30.0 * 30.0)


# --------------------------------------------------------------------------------------
# iAUC con valor analitico conocido
# --------------------------------------------------------------------------------------


def test_iauc_triangle_matches_analytic_area():
    """Triangulo de base 120 min y altura 60 mg/dL -> area = 0.5*120*60 = 3600."""
    pts = triangular(baseline=90.0, amplitude=60.0, ttp=60.0, width=120.0, res=15)
    m = compute_metrics(make_window(pts), CFG, post_window_min=180)

    assert m is not None
    assert m.baseline_mgdl == pytest.approx(90.0)
    assert m.iauc_120 == pytest.approx(3600.0)
    assert m.peak_delta_mgdl == pytest.approx(60.0)
    assert m.time_to_peak_min == pytest.approx(60.0)
    assert m.time_above_baseline_min == pytest.approx(120.0)


@pytest.mark.parametrize("res", [1, 5, 15])
def test_iauc_invariant_to_sampling_resolution(res: int):
    """La misma curva muestreada a 1, 5 o 15 min da el mismo iAUC.

    Es exacto porque la curva es lineal a trozos y el trapecio es exacto para eso.
    Confirma que no hay sesgo introducido por la resolucion en el AREA (el PICO si se
    ve afectado; ver el test siguiente).
    """
    pts = triangular(baseline=90.0, amplitude=60.0, ttp=60.0, width=120.0, res=res)
    m = compute_metrics(make_window(pts, resolution_min=res), CFG, post_window_min=180)
    assert m is not None
    assert m.iauc_120 == pytest.approx(3600.0, rel=1e-9)


def test_peak_flagged_as_underestimated_only_at_coarse_resolution():
    pts = triangular(90.0, 60.0, 60.0, 120.0, res=15)
    coarse = compute_metrics(make_window(pts, resolution_min=15), CFG, post_window_min=180)
    fine = compute_metrics(make_window(pts, resolution_min=5), CFG, post_window_min=180)
    assert coarse is not None and fine is not None
    assert coarse.peak_underestimated is True
    assert fine.peak_underestimated is False


def test_iauc_works_when_meal_falls_between_samples():
    """El caso REAL: a 15 min de resolucion la comida casi nunca cae sobre una muestra.

    Regresion: una version previa exigia una muestra exacta en t=0 y habria descartado
    practicamente todas las ventanas reales del FreeStyle Libre 2 Plus.
    """

    # Rampa 90 -> 150 entre t=-7 y t=53, bajada a 90 en t=113. Muestras cada 15 min
    # desplazadas 7 min: ninguna cae en t=0, t=120 ni t=180.
    def curve(t: float) -> float:
        if t <= -7.0:
            return 90.0
        if t <= 53.0:
            return 90.0 + 60.0 * (t + 7.0) / 60.0
        if t <= 113.0:
            return 150.0 - 60.0 * (t - 53.0) / 60.0
        return 90.0

    pts = [(float(t), curve(float(t))) for t in np.arange(-37.0, 196.0, 15.0)]
    offsets = [p[0] for p in pts]
    assert 0.0 not in offsets and 120.0 not in offsets and 180.0 not in offsets

    m = compute_metrics(make_window(pts), CFG, post_window_min=180)
    assert m is not None
    assert m.iauc_120 is not None
    assert m.iauc_180 is not None
    # Area analitica del triangulo completo (base 120 min, altura 60) = 3600, y esta
    # enteramente dentro de [0, 120] salvo el trocito previo a t=0.
    assert m.iauc_120 == pytest.approx(3600.0 - 0.5 * 7.0 * 7.0, rel=0.02)
    assert m.iauc_180 == pytest.approx(m.iauc_120, rel=1e-9)


def test_iauc_180_is_none_when_data_does_not_reach_180():
    """Preferimos no reportar antes que extrapolar."""
    pts = triangular(90.0, 60.0, 60.0, 120.0, res=15)
    m = compute_metrics(make_window(pts), CFG, post_window_min=180)
    assert m is not None
    assert m.iauc_120 is not None
    assert m.iauc_180 is None


def test_flat_curve():
    # Muestras no alineadas con la comida (caso realista a 15 min): no hay punto en t=0.
    pts = [(float(t), 92.0) for t in np.arange(-35.0, 196.0, 15.0)]
    assert 0.0 not in [p[0] for p in pts]
    m = compute_metrics(make_window(pts), CFG, post_window_min=180)
    assert m is not None
    assert m.iauc_120 == pytest.approx(0.0)
    assert m.peak_delta_mgdl == pytest.approx(0.0)
    assert m.curve_shape is CurveShape.FLAT
    assert m.cv_pct == pytest.approx(0.0)


def test_net_auc_can_be_negative_while_positive_iauc_is_zero():
    """Una comida seguida de bajada: iAUC positivo 0, neto negativo. Distinguirlos importa."""
    pts = [(t, 95.0) for t in np.arange(-35.0, 0.0, 15.0)]
    pts += [(float(t), 95.0 - 10.0 * min(t, 60) / 60.0) for t in np.arange(0.0, 181.0, 15.0)]
    m = compute_metrics(make_window(pts), CFG, post_window_min=180)
    assert m is not None
    assert m.iauc_120 == pytest.approx(0.0)
    assert m.iauc_net_120 is not None and m.iauc_net_120 < 0


def test_biphasic_curve_detected():
    baseline = 90.0
    shape = [0, 20, 55, 40, 25, 48, 30, 12, 4, 0]
    pts = [(t, baseline) for t in np.arange(-35.0, 0.0, 15.0)]
    pts += [(float(i * 20), baseline + d) for i, d in enumerate(shape)]
    m = compute_metrics(make_window(pts, resolution_min=20), CFG, post_window_min=180)
    assert m is not None
    assert m.curve_shape is CurveShape.BIPHASIC


def test_time_to_return_to_baseline():
    """Baja de basal+10 exactamente a los 105 min por interpolacion lineal."""
    pts = [(t, 90.0) for t in np.arange(-35.0, 0.0, 15.0)]
    pts += [(0.0, 90.0), (60.0, 150.0), (120.0, 90.0), (180.0, 90.0)]
    m = compute_metrics(make_window(pts), CFG, post_window_min=180)
    assert m is not None
    # Bajada lineal 150 -> 90 entre t=60 y t=120; cruza 100 en t = 60 + 60*(50/60) = 110
    assert m.time_to_return_baseline_min == pytest.approx(110.0)


# --------------------------------------------------------------------------------------
# Basal
# --------------------------------------------------------------------------------------


def test_baseline_uses_median_not_mean():
    """Un artefacto en la ventana previa no debe mover la basal."""
    pts = [(-35.0, 92.0), (-20.0, 94.0), (-5.0, 250.0)]  # 250 = artefacto
    pts += [(float(t), 95.0) for t in np.arange(0.0, 181.0, 15.0)]
    base = compute_baseline(make_window(pts), CFG)
    assert base is not None
    median, _, n = base
    assert median == pytest.approx(94.0)
    assert n == 3
    assert median != pytest.approx(np.mean([92.0, 94.0, 250.0]))


def test_no_baseline_returns_none():
    pts = [(float(t), 95.0) for t in np.arange(0.0, 181.0, 15.0)]
    assert compute_baseline(make_window(pts), CFG) is None
    assert compute_metrics(make_window(pts), CFG, post_window_min=180) is None


def test_baseline_window_widens_with_resolution():
    """A 15 min la ventana de basal es 30 min; a 5 min, 20 min."""
    assert CFG.baseline_window_min(15) == 30
    assert CFG.baseline_window_min(5) == 20
    assert CFG.max_gap_min(15) == pytest.approx(37.5)
    assert CFG.max_gap_min(5) == pytest.approx(20.0)


# --------------------------------------------------------------------------------------
# Cobertura y resolucion
# --------------------------------------------------------------------------------------


def test_detect_resolution_is_robust_to_gaps():
    base = np.datetime64("2026-08-01T00:00:00")
    steps = [0, 15, 30, 45, 60, 600, 615, 630]  # hueco de 9 h en medio
    ts = np.array([base + np.timedelta64(s, "m") for s in steps])
    assert detect_resolution_min(ts) == 15


def test_detect_resolution_dexcom_five_minutes():
    base = np.datetime64("2026-08-01T00:00:00")
    ts = np.array([base + np.timedelta64(5 * i, "m") for i in range(20)])
    assert detect_resolution_min(ts) == 5


def test_coverage_counts_edge_gaps():
    """Datos que empiezan tarde: es un hueco real aunque no haya dos muestras alrededor."""
    pts = [(float(t), 95.0) for t in np.arange(60.0, 181.0, 15.0)]
    m = compute_metrics(make_window(pts), CFG, post_window_min=180)
    assert m is None  # sin basal
    from glycomind.analysis.metrics import compute_coverage

    coverage, max_gap = compute_coverage(make_window(pts), CFG, 180)
    assert max_gap >= 60.0
    assert coverage < 100.0


# --------------------------------------------------------------------------------------
# Propiedades
# --------------------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.floats(min_value=40.0, max_value=350.0, allow_nan=False),
        min_size=13,
        max_size=13,
    )
)
def test_positive_iauc_never_below_net_iauc(values: list[float]):
    """Invariante: el area positiva siempre domina al area neta."""
    t = np.arange(0.0, 13 * 15.0, 15.0)
    v = np.array(values)
    baseline = float(np.median(v))
    d = v - baseline
    pos, time_above = _positive_area_and_time(t, d)
    net = float(np.trapezoid(d, t))
    assert pos >= net - 1e-6
    assert pos >= 0.0
    assert 0.0 <= time_above <= t[-1] + 1e-9


@settings(max_examples=100, deadline=None)
@given(st.floats(min_value=1.0, max_value=100.0))
def test_iauc_scales_linearly_with_amplitude(amplitude: float):
    pts = triangular(90.0, amplitude, 60.0, 120.0, res=15)
    m = compute_metrics(make_window(pts), CFG, post_window_min=180)
    assert m is not None
    assert m.iauc_120 == pytest.approx(0.5 * 120.0 * amplitude, rel=1e-6)
