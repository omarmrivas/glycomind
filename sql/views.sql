-- Vistas de lectura para Grafana y exploracion.
-- Idempotente: se puede reaplicar sin efectos.
--
-- Nota de diseno: la vista v_food_summary NO expone una media sin mas. Incrusta el
-- umbral de suficiencia de evidencia en SQL, porque la tentacion de leer "arroz = +58"
-- desde n=2 es enorme y el dato no lo soporta (ICC intraindividual 0.14-0.31 medido en
-- comidas duplicadas; ver docs/02-nucleo-cientifico.md).

CREATE OR REPLACE VIEW v_meal_response AS
SELECT
    m.user_id,
    m.id                                            AS meal_id,
    m.consumed_at,
    m.consumed_at + make_interval(mins => m.tz_offset_min) AS consumed_at_local,
    EXTRACT(HOUR FROM m.consumed_at + make_interval(mins => m.tz_offset_min))::int AS hour_local,
    m.meal_type,
    m.source                                        AS capture_source,
    m.free_text,
    m.entry_completeness,
    r.quality,
    r.exclusion_reasons,
    r.degradation_reasons,
    r.baseline_mgdl,
    r.peak_mgdl,
    r.peak_delta_mgdl,
    r.peak_underestimated,
    r.time_to_peak_min,
    r.iauc_120,
    r.iauc_180,
    r.time_above_baseline_min,
    r.time_to_return_baseline_min,
    r.curve_shape,
    r.coverage_pct,
    r.max_gap_min,
    r.resolution_min,
    r.prev_meal_gap_min,
    r.next_meal_gap_min,
    r.sensor_age_hours,
    r.vendor,
    r.algorithm_version
FROM meal m
LEFT JOIN meal_glucose_response r ON r.meal_id = m.id;


-- LA metrica de producto de la Fase 1. Si cae de ~60%, el cuello de botella es la
-- captura de comidas o la adherencia al sensor, no la estadistica.
CREATE OR REPLACE VIEW v_pairing_quality_daily AS
SELECT
    user_id,
    date_trunc('day', consumed_at_local)::date      AS day_local,
    count(*)                                        AS meals,
    count(*) FILTER (WHERE quality IN ('ok', 'degraded')) AS usable,
    count(*) FILTER (WHERE quality = 'excluded')    AS excluded,
    round(
        100.0 * count(*) FILTER (WHERE quality IN ('ok', 'degraded'))
        / NULLIF(count(*), 0), 1
    )                                               AS pairing_valid_pct
FROM v_meal_response
GROUP BY 1, 2
ORDER BY 1, 2;


-- Desglose de exclusiones: es el mapa de que hay que arreglar en la UX de registro.
CREATE OR REPLACE VIEW v_exclusion_reasons AS
SELECT
    user_id,
    unnest(exclusion_reasons)                       AS reason,
    count(*)                                        AS n
FROM meal_glucose_response
WHERE quality = 'excluded'
GROUP BY 1, 2
ORDER BY 1, 3 DESC;


-- Cobertura del CGM por dia. Los puntos esperados se derivan de la resolucion NATIVA
-- de cada sesion (15 min en FreeStyle Libre 2 Plus, 5 en Dexcom), nunca de una
-- constante.
CREATE OR REPLACE VIEW v_cgm_coverage_daily AS
SELECT
    g.user_id,
    date_trunc('day', g.ts_utc + make_interval(mins => g.tz_offset_min))::date AS day_local,
    count(*)                                        AS readings,
    round(
        100.0 * count(*) / NULLIF(1440.0 / max(s.native_resolution_min), 0), 1
    )                                               AS coverage_pct,
    count(*) FILTER (WHERE g.quality_flags <> 0)    AS flagged,
    max(s.native_resolution_min)                    AS resolution_min
FROM glucose_reading g
JOIN cgm_sensor_session s ON s.id = g.session_id
WHERE g.source_record = 'historic'
GROUP BY 1, 2
ORDER BY 1, 2;


-- Resumen por alimento. Deliberadamente cauteloso.
--
-- 'evidence_status' implementa la regla que separa este sistema de una app de CGM
-- cualquiera: por debajo de 8 exposiciones validas NO hay hallazgo, solo observaciones.
-- El agrupado por texto libre es provisional: en la Fase 2 se agrupa por food_id
-- canonico.
CREATE OR REPLACE VIEW v_food_summary AS
WITH valid AS (
    SELECT user_id, lower(trim(free_text)) AS food_label, iauc_120, peak_delta_mgdl
    FROM v_meal_response
    WHERE quality IN ('ok', 'degraded')
      AND iauc_120 IS NOT NULL
      AND free_text IS NOT NULL
      AND trim(free_text) <> ''
)
SELECT
    user_id,
    food_label,
    count(*)                                                          AS n_exposures,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY iauc_120)::numeric, 0) AS iauc_120_median,
    round(percentile_cont(0.25) WITHIN GROUP (ORDER BY iauc_120)::numeric, 0) AS iauc_120_q1,
    round(percentile_cont(0.75) WITHIN GROUP (ORDER BY iauc_120)::numeric, 0) AS iauc_120_q3,
    round(min(iauc_120)::numeric, 0)                                  AS iauc_120_min,
    round(max(iauc_120)::numeric, 0)                                  AS iauc_120_max,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY peak_delta_mgdl)::numeric, 0) AS peak_delta_median,
    CASE
        WHEN count(*) >= 8 THEN 'suficiente_para_observacion'
        WHEN count(*) >= 4 THEN 'insuficiente_tendencia_no_concluyente'
        ELSE 'insuficiente_no_interpretar'
    END                                                               AS evidence_status
FROM valid
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
