"""Metricas de respuesta postprandial. Determinista, versionado, testeado.

Ninguna de estas funciones usa un LLM ni tiene aleatoriedad: dado el mismo input y la
misma ``ALGORITHM_VERSION``, el resultado es identico bit a bit. Es el requisito que
hace auditable al sistema entero.

Decisiones de metodo que no son obvias:

* El **iAUC se calcula sobre los tiempos reales de muestreo**, no sobre una rejilla
  remuestreada. Interpolar linealmente y luego aplicar la regla del trapecio da
  exactamente el mismo area: remuestrear solo anade puntos falsos y da una sensacion
  de precision que no existe.
* Los segmentos que **cruzan la basal se parten en el cruce exacto**. Truncar a nivel de
  muestra (``max(g - b, 0)`` punto a punto) sobreestima el area positiva.
* Todos los umbrales dependen de la **resolucion nativa detectada**. El FreeStyle Libre
  2 Plus exporta a 15 min y Dexcom a 5 min; una constante rompe uno de los dos.
"""

from __future__ import annotations

import numpy as np

from glycomind.config import AnalysisSettings
from glycomind.domain.enums import CurveShape
from glycomind.domain.models import ResponseMetrics, WindowSeries

_MIN_POINTS_FOR_SHAPE = 5


def detect_resolution_min(ts_utc: np.ndarray) -> int:
    """Resolucion nativa = mediana de las diferencias consecutivas, en minutos.

    Se detecta, no se asume. Los huecos grandes no la afectan porque la mediana es
    robusta, pero ademas se filtran explicitamente los saltos > 60 min para que un
    export con dias faltantes no infle el resultado.
    """
    if len(ts_utc) < 2:
        raise ValueError("se necesitan al menos 2 lecturas para detectar la resolucion")
    secs = ts_utc.astype("datetime64[s]").astype(np.int64)
    diffs = np.diff(secs) / 60.0
    diffs = diffs[(diffs > 0) & (diffs <= 60)]
    if diffs.size == 0:
        raise ValueError("no hay intervalos plausibles para detectar la resolucion")
    return max(1, round(float(np.median(diffs))))


def _clip_with_interpolation(
    rel: np.ndarray, vals: np.ndarray, lo: float, hi: float
) -> tuple[np.ndarray, np.ndarray] | None:
    """Recorta a [lo, hi] interpolando linealmente en los extremos exactos.

    Devuelve ``None`` si la serie no cubre el intervalo completo: preferimos no
    reportar un iAUC antes que reportar uno extrapolado.
    """
    if rel.size < 2 or rel[0] > lo or rel[-1] < hi:
        return None
    inner = (rel > lo) & (rel < hi)
    t = np.concatenate(([lo], rel[inner], [hi]))
    v = np.concatenate(
        ([float(np.interp(lo, rel, vals))], vals[inner], [float(np.interp(hi, rel, vals))])
    )
    return t, v


def _positive_area_and_time(t: np.ndarray, d: np.ndarray) -> tuple[float, float]:
    """Integral de ``max(d, 0)`` y tiempo con ``d > 0``, partiendo en los cruces por cero.

    ``d`` son incrementos sobre la basal. La particion exacta en el cruce es lo que
    diferencia un iAUC correcto de uno inflado.
    """
    area = 0.0
    time_above = 0.0
    for i in range(len(t) - 1):
        t1, t2 = float(t[i]), float(t[i + 1])
        d1, d2 = float(d[i]), float(d[i + 1])
        dt = t2 - t1
        if dt <= 0:
            continue
        if d1 >= 0 and d2 >= 0:
            area += 0.5 * (d1 + d2) * dt
            time_above += dt
        elif d1 <= 0 and d2 <= 0:
            continue
        elif d1 > 0 > d2:
            frac = d1 / (d1 - d2)  # fraccion del segmento que queda por encima
            area += 0.5 * d1 * frac * dt
            time_above += frac * dt
        else:  # d1 < 0 < d2
            frac = d2 / (d2 - d1)
            area += 0.5 * d2 * frac * dt
            time_above += frac * dt
    return area, time_above


def _local_maxima_with_prominence(vals: np.ndarray, min_prominence: float) -> list[int]:
    """Maximos locales cuya prominencia topografica alcanza el umbral.

    Prominencia = altura del pico menos el punto mas alto de los dos valles que hay que
    cruzar para llegar a un punto mas alto. Evita contar como 'segundo pico' cualquier
    rizo del sensor.
    """
    n = len(vals)
    out: list[int] = []
    for i in range(1, n - 1):
        if not (vals[i] >= vals[i - 1] and vals[i] >= vals[i + 1]):
            continue
        if vals[i] == vals[i - 1] and vals[i] == vals[i + 1]:
            continue
        left_min = vals[i]
        j = i - 1
        while j >= 0 and vals[j] <= vals[i]:
            left_min = min(left_min, vals[j])
            j -= 1
        right_min = vals[i]
        k = i + 1
        while k < n and vals[k] <= vals[i]:
            right_min = min(right_min, vals[k])
            k += 1
        if vals[i] - max(left_min, right_min) >= min_prominence:
            out.append(i)
    return out


def compute_baseline(
    window: WindowSeries, cfg: AnalysisSettings
) -> tuple[float, float | None, int] | None:
    """Basal preprandial: mediana de la ventana previa.

    Mediana y no media: con 2-3 puntos a 15 min de resolucion, un solo artefacto
    desplazaria la media y con ella TODAS las metricas derivadas.
    """
    width = cfg.baseline_window_min(window.resolution_min)
    lo = -(cfg.baseline_offset_min + width)
    hi = -cfg.baseline_offset_min
    pre = window.slice(lo, hi)
    if len(pre) == 0:
        return None
    vals = pre.values
    sd = float(np.std(vals, ddof=1)) if len(vals) >= 2 else None
    return float(np.median(vals)), sd, len(vals)


def compute_coverage(
    window: WindowSeries, cfg: AnalysisSettings, post_window_min: int
) -> tuple[float, float]:
    """(cobertura %, hueco maximo en min) sobre [-pre, +post].

    El hueco maximo incluye los bordes: si los datos empiezan 40 min despues del inicio
    de la ventana, eso es un hueco real aunque no haya dos muestras que lo delimiten.
    """
    res = window.resolution_min
    lo = -cfg.pre_window_min(res)
    hi = float(post_window_min)
    seg = window.slice(lo, hi)
    span = hi - lo
    expected = span / res + 1
    coverage = min(100.0, 100.0 * len(seg) / expected) if expected > 0 else 0.0
    if len(seg) == 0:
        return 0.0, span
    edges = np.concatenate(([lo], seg.rel_min, [hi]))
    return coverage, float(np.max(np.diff(edges)))


def compute_metrics(
    window: WindowSeries,
    cfg: AnalysisSettings,
    *,
    post_window_min: int,
) -> ResponseMetrics | None:
    """Calcula todas las metricas de una ventana postprandial.

    El llamador debe entregar una ventana que **se extienda al menos una muestra mas alla
    de** ``post_window_min``; si no, ``iauc_180`` sera ``None`` porque no se puede
    interpolar el valor exacto en el borde y no extrapolamos.

    Devuelve ``None`` si no hay basal computable: sin basal no hay iAUC, y un iAUC sin
    basal fiable es peor que ninguno.
    """
    res = window.resolution_min
    base = compute_baseline(window, cfg)
    if base is None:
        return None
    baseline, baseline_sd, baseline_n = base

    post = window.slice(0.0, float(post_window_min))
    if len(post) < 2:
        return None

    rel, vals = post.rel_min, post.values

    # --- pico ---
    idx = int(np.argmax(vals))
    peak = float(vals[idx])
    peak_delta = peak - baseline
    ttp = float(rel[idx])
    # Con resolucion > 5 min el apex real cae entre muestras: es una COTA INFERIOR.
    peak_underestimated = res > cfg.peak_reliable_max_resolution_min

    # --- iAUC a horizontes fijos ---
    # Se integra sobre la ventana COMPLETA (incluidos los puntos preprandiales), no sobre
    # el recorte a [0, post]: la comida casi nunca cae encima de una muestra, asi que el
    # valor en t=0 hay que interpolarlo entre la ultima muestra previa y la primera
    # posterior. Exigir una muestra exacta en t=0 descartaria casi todas las ventanas
    # reales a 15 min de resolucion.
    full_rel, full_vals = window.rel_min, window.values

    def _iauc(hi: float) -> tuple[float | None, float | None, float | None]:
        clipped = _clip_with_interpolation(full_rel, full_vals, 0.0, hi)
        if clipped is None:
            return None, None, None
        t, v = clipped
        d = v - baseline
        pos, _ = _positive_area_and_time(t, d)
        net = float(np.trapezoid(d, t))
        total = float(np.trapezoid(v, t))
        return pos, net, total

    iauc_120, iauc_net_120, auc_total_120 = _iauc(120.0)
    iauc_180 = _iauc(180.0)[0] if post_window_min >= 180 else None

    # --- tiempo por encima de basal, hasta donde alcancen los datos ---
    hi_avail = min(float(post_window_min), float(full_rel[-1]))
    clipped_avail = _clip_with_interpolation(full_rel, full_vals, 0.0, hi_avail)
    if clipped_avail is not None:
        t_av, v_av = clipped_avail
        _, time_above = _positive_area_and_time(t_av, v_av - baseline)
    else:
        _, time_above = _positive_area_and_time(rel, vals - baseline)

    # --- retorno a basal ---
    threshold = baseline + cfg.return_to_baseline_margin_mgdl
    ttr = _time_to_return(rel, vals, threshold, after=ttp, sustained_min=cfg.min_sustained_min(res))

    # --- variabilidad y forma ---
    mean_v = float(np.mean(vals))
    cv = float(np.std(vals, ddof=1) / mean_v * 100.0) if len(vals) >= 2 and mean_v > 0 else 0.0
    shape = _classify_shape(rel, vals, baseline, peak_delta, cfg)

    coverage, max_gap = compute_coverage(window, cfg, post_window_min)

    return ResponseMetrics(
        baseline_mgdl=baseline,
        baseline_sd=baseline_sd,
        baseline_n=baseline_n,
        peak_mgdl=peak,
        peak_delta_mgdl=peak_delta,
        time_to_peak_min=ttp,
        peak_underestimated=peak_underestimated,
        iauc_120=iauc_120,
        iauc_180=iauc_180,
        iauc_net_120=iauc_net_120,
        auc_total_120=auc_total_120,
        time_above_baseline_min=time_above,
        time_to_return_baseline_min=ttr,
        cv_pct=cv,
        curve_shape=shape,
        coverage_pct=coverage,
        max_gap_min=max_gap,
        n_points=len(post),
        resolution_min=res,
    )


def _time_to_return(
    rel: np.ndarray,
    vals: np.ndarray,
    threshold: float,
    *,
    after: float,
    sustained_min: float,
) -> float | None:
    """Primer instante tras el pico en que la glucosa baja del umbral y se mantiene."""
    for i in range(len(rel) - 1):
        t1, t2 = float(rel[i]), float(rel[i + 1])
        if t2 <= after:
            continue
        v1, v2 = float(vals[i]), float(vals[i + 1])
        if v1 > threshold >= v2:
            frac = (v1 - threshold) / (v1 - v2) if v1 != v2 else 0.0
            tc = t1 + frac * (t2 - t1)
            if tc < after:
                continue
            # "Sostenido": las muestras del horizonte siguiente tambien por debajo. Si no
            # hay muestras (fin de ventana) se acepta el cruce, no hay mas informacion.
            horizon = vals[(rel > tc) & (rel <= tc + sustained_min)]
            if horizon.size == 0 or bool(np.all(horizon <= threshold)):
                return tc
        elif t1 >= after and v1 <= threshold:
            return t1
    return None


def _classify_shape(
    rel: np.ndarray,
    vals: np.ndarray,
    baseline: float,
    peak_delta: float,
    cfg: AnalysisSettings,
) -> CurveShape:
    if len(vals) < _MIN_POINTS_FOR_SHAPE:
        return CurveShape.UNKNOWN
    if peak_delta < cfg.flat_curve_threshold_mgdl:
        return CurveShape.FLAT
    prominence = max(10.0, 0.25 * peak_delta)
    peaks = _local_maxima_with_prominence(vals, prominence)
    if len(peaks) >= 2:
        return CurveShape.BIPHASIC
    plateau_threshold = baseline + peak_delta * 0.9
    _, time_near_peak = _positive_area_and_time(rel, vals - plateau_threshold)
    return CurveShape.PLATEAU if time_near_peak > 45.0 else CurveShape.MONOPHASIC
