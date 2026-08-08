# GlycoMind

Sistema para entender la relación entre **lo que come una persona concreta** y **su
respuesta glucémica**, medida con un sensor continuo de glucosa (CGM).

> **Estado: Fase 1 — pipeline determinista, funcionando.**
> Ingesta de LibreView → control de calidad de señal → emparejamiento comida/ventana →
> métricas postprandiales. Sin LLM en la ruta crítica, y a propósito.
>
> El diseño completo (fases 2–5, RAG científico, agentes, recomendaciones, regulación)
> está en [`docs/`](docs/README.md).

---

## Por qué el orden es este

La Fase 1 no incluye IA. No es una omisión: es la decisión central del proyecto.

Un estudio con 1,189 respuestas a **comidas duplicadas** (la misma comida, a la misma
persona, con una semana de diferencia, en régimen intrahospitalario) midió una fiabilidad
intraindividual del iAUC de **ICC = 0.31 (Abbott)** y **0.14 (Dexcom)**
([AJCN 2024](https://pubmed.ncbi.nlm.nih.gov/37503002/)). Es decir: **entre el 69% y el
86% de la variación que se observa en una comida individual no es atribuible a la
comida.**

Consecuencia práctica: "arroz → +58 mg/dL", medido una vez, no es información. Cualquier
capa de recomendación construida sobre mediciones sucias produce afirmaciones con
confianza estadística cero y voz autoritaria. Por eso lo primero que hay que construir es
la tabla `meal_glucose_response` y su honestidad sobre lo que **no** se puede afirmar.

---

## Qué hace hoy

- **Importa** el CSV que exportas de LibreView (vía oficial de Abbott, con reCAPTCHA:
  es manual por diseño, no se puede automatizar).
- **Detecta la resolución nativa** del sensor en vez de asumirla. El FreeStyle Libre 2
  Plus mide cada minuto pero **almacena y exporta cada 15 min**; Dexcom da 5 min. Todos
  los umbrales de calidad se derivan de la resolución detectada.
- **Detecta sesiones de sensor** por número de serie, y excluye las primeras 12 h de cada
  una (error elevado documentado al inicio de vida del sensor).
- **Marca artefactos** sin borrarlos: fuera de rango fisiológico, tasa de cambio
  imposible, *compression lows* nocturnos, escalón entre sensores.
- **Empareja** cada comida con su ventana glucémica, o explica por qué no puede.
- **Calcula** basal, Δ pico, tiempo al pico, iAUC-120/180, tiempo sobre basal, retorno a
  basal, CV y forma de curva — con partición exacta en los cruces de la basal.
- **Reporta `pairing_valid_ratio`**, la métrica de producto de esta fase.

Lo que **no** hace todavía: visión de alimentos, modelo bayesiano, claims personales,
RAG científico, recomendaciones. Están diseñados en `docs/`, no implementados.

---

## Arranque rápido

```bash
docker compose up -d postgres
```

```bash
cp .env.example .env && uv sync --all-groups && uv run alembic upgrade head
```

```bash
uv run glycomind db apply-views
```

Con datos sintéticos, para ver el pipeline entero funcionando sin esperar 14 días de sensor:

```bash
uv run python scripts/make_demo_csv.py data/demo.csv
```

```bash
uv run glycomind user create --email tu@correo.com --timezone America/Mexico_City
```

```bash
uv run glycomind import-libreview data/demo.csv --user tu@correo.com
```

```bash
uv run glycomind meal import data/demo.meals.txt --user tu@correo.com
```

```bash
uv run glycomind analyze --user tu@correo.com
```

```bash
uv run glycomind report --user tu@correo.com
```

API y dashboards:

```bash
uv run uvicorn glycomind.api.main:app --reload
```

```bash
docker compose up -d grafana
```

---

## Con datos reales

1. Entra a [libreview.com](https://www.libreview.com) con tus credenciales de LibreLink.
2. **Historial de glucosa** (arriba a la izquierda) → **Descargar datos de glucosa**
   (arriba a la derecha) → resolver el reCAPTCHA → **Descargar**.
3. **No abras el CSV en Excel.** Abbott advierte que volver a guardarlo cambia el formato.
   El parser detecta los síntomas (separador `;`, coma decimal, fechas reformateadas) y
   avisa, pero es mejor no arriesgarse.
4. `uv run glycomind import-libreview <archivo> --user <correo>`

Reimportar rangos solapados es seguro: la ingesta es idempotente por
`(user_id, ts_utc, session_id)` y te dice cuántas lecturas son realmente nuevas.

**Si el import falla diciendo que no puede determinar el orden de fecha**, exporta un
rango más largo (con al menos un día > 12) o pasa `--date-order DMY`. El parser prefiere
fallar antes que adivinar: equivocarse desplaza tus comidas hasta 11 meses.

---

## Estructura

```
src/glycomind/
  config.py              Umbrales de análisis + ALGORITHM_VERSION
  domain/                Enums y objetos de valor inmutables
  db/                    Modelos SQLAlchemy (fuente de verdad del esquema)
  ingest/
    libreview.py         Parser del CSV: locales, formatos de fecha, unidades
    sessions.py          Detección de sesiones de sensor por nº de serie
    service.py           Persistencia idempotente
  analysis/
    quality.py           QC de señal: marca, nunca borra
    metrics.py           iAUC, pico, TTP, forma de curva  ← el núcleo
    pairing.py           ¿esta ventana es atribuible a esta comida?
    pipeline.py          Orquestación → meal_glucose_response
  api/main.py            API de lectura (FastAPI)
  cli.py                 CLI
sql/views.sql            Vistas para Grafana
docs/                    Diseño completo del sistema (fases 1–5)
```

---

## Desarrollo

```bash
uv run pytest -q
```

```bash
uv run ruff check . && uv run ruff format --check .
```

Los tests de `test_integration.py` necesitan PostgreSQL; se omiten solos si no está.

### Dos reglas del repo

1. **Ninguna métrica derivada se expone sin su contexto.** Todo endpoint y toda tabla
   devuelven `n`, calidad, incertidumbre y `algorithm_version` junto al número. Es lo que
   obliga a la interfaz —y más adelante al LLM— a enfrentarse al ruido en vez de
   esconderlo.
2. **Cambiar un umbral de análisis obliga a subir `ALGORITHM_VERSION`** en
   `config.py` y a recalcular. Sin eso el histórico deja de ser auditable.

---

## Límites

Esto es una **herramienta de bienestar general y autoconocimiento**. No diagnostica, no
predice glucosa, no calcula dosis de insulina y no sustituye a un profesional. La
frontera con "dispositivo médico" y el marco de privacidad aplicable (LFPDPPP 2025,
GDPR, COFEPRIS) están en [`docs/08-regulatorio.md`](docs/08-regulatorio.md).

Los datos de glucosa, comidas, peso y síntomas son **datos personales sensibles** bajo la
ley mexicana vigente desde marzo de 2025, con sanciones duplicadas. `data/` y `*.csv`
están en `.gitignore` por ese motivo.
