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
- **Resuelve el texto libre a un catálogo canónico de alimentos** (78 alimentos mexicanos,
  266 alias, 12 platillos descompuestos en ingredientes), para que el análisis agrupe por
  identidad y no por cadena de texto.

Lo que **no** hace todavía: visión de alimentos, modelo bayesiano, claims personales,
RAG científico, recomendaciones. Están diseñados en `docs/`, no implementados.

---

## Catálogo de alimentos

Sin catálogo, `"tortillas"` y `"tortillas, frijoles, pollo"` son grupos distintos y no se
puede aprender nada. El catálogo convierte texto libre en `food_id`, que es lo que el
modelo jerárquico de la Fase 2 necesita como covariable.

```bash
docker compose exec api glycomind food seed
```

```bash
docker compose exec api glycomind food link --user tu@correo.com
```

Probar el resolutor sin escribir nada:

```bash
docker compose exec api glycomind food search "2 tortillas de maíz con frijoles y aguacate"
```

### Dos reglas del resolutor

1. **Un emparejamiento aproximado nunca se asigna solo.** Solo los alias exactos se
   aplican automáticamente; lo demás se propone y espera confirmación. Un alimento mal
   asignado corrompe el modelo estadístico en silencio, y el silencio es peor que el hueco.
   Los items dudosos quedan con `food_id` nulo y su sugerencia visible en
   `glycomind food pending`.
2. **El texto crudo del usuario nunca se pierde.** `raw_label` se conserva siempre; lo
   que se guarda junto a él son interpretaciones anotadas con su confianza y su método.

Confirmar un item enseña un alias propio del usuario, así que la próxima vez esa misma
etiqueta se resuelve sola y de forma exacta. El catálogo mejora con el uso.

### Macronutrientes

**El catálogo semilla no trae macros.** Escribirlos a mano sería inventar datos
nutricionales. Se importan de USDA FoodData Central, y cada valor queda con su `fdcId`
en `food_source_map`:

```bash
docker compose exec api glycomind food import-nutrients --api-key TU_CLAVE
```

La clave es gratuita en [api.data.gov/signup](https://api.data.gov/signup/). Límite de la
API: 1,000 peticiones por hora.

⚠️ FoodData Central cubre mal la cocina mexicana casera. Por eso los platillos compuestos
(chilaquiles, pozole, tacos) se modelan como **recetas con ingredientes**: los
ingredientes sí están cubiertos, y descomponerlos es lo que permite aprender qué
*componente* mueve la glucosa. Integrar las tablas mexicanas (INNSZ, SMAE, IMSS, Tabla
extendida 2019) es el siguiente paso.

---

## Arranque rápido

Un solo comando, idéntico en Windows, macOS y Linux. Lo único que necesitas es Docker.

```bash
docker compose up -d
```

Eso levanta Postgres, aplica las migraciones y las vistas, y arranca la API. **No hace
falta crear `.env`**: cada variable tiene su valor por defecto en `docker-compose.yml`.
La API no arranca hasta que las migraciones terminan con éxito, así que nunca se sirve
contra un esquema viejo.

| Servicio | URL |
|---|---|
| API + documentación interactiva | http://localhost:8000/docs |
| Grafana | http://localhost:3000 (`admin` / `admin`) |
| MinIO | http://localhost:9001 |

### Ver el pipeline funcionando sin esperar 14 días de sensor

```bash
docker compose run --rm demo
```

Genera datos sintéticos, los importa, los analiza e imprime el reporte. Está bajo un
perfil aparte a propósito: son datos falsos y no deben mezclarse con los reales por
accidente. Repetir el comando es inocuo — de hecho es una buena forma de comprobar que
la ingesta es idempotente.

### Usar el CLI

Se ejecuta dentro del contenedor de la API, así que no depende del sistema operativo
ni de tener Python instalado:

```bash
docker compose exec api glycomind --help
```

```bash
docker compose exec api glycomind user create --email tu@correo.com --timezone America/Mexico_City
```

Los archivos se intercambian por la carpeta `./data`, que está montada dentro del
contenedor (y está en `.gitignore`):

```bash
docker compose exec api glycomind import-libreview data/tu-export.csv --user tu@correo.com
```

```bash
docker compose exec api glycomind analyze --user tu@correo.com
```

Para parar todo:

```bash
docker compose down
```

Añade `-v` a ese último comando si además quieres borrar la base de datos.

---

## Con datos reales

1. Entra a [libreview.com](https://www.libreview.com) con tus credenciales de LibreLink.
2. **Historial de glucosa** (arriba a la izquierda) → **Descargar datos de glucosa**
   (arriba a la derecha) → resolver el reCAPTCHA → **Descargar**.
3. **No abras el CSV en Excel.** Abbott advierte que volver a guardarlo cambia el formato.
   El parser detecta los síntomas (separador `;`, coma decimal, fechas reformateadas) y
   avisa, pero es mejor no arriesgarse.
4. Copia el CSV a la carpeta `data/` del repo y ejecuta:

```bash
docker compose exec api glycomind import-libreview data/tu-export.csv --user tu@correo.com
```

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
  catalog/
    text.py              Parseo de texto libre: cantidades, separadores, preparación
    resolver.py          Texto → alimento canónico (exacto + difuso con pg_trgm)
    loader.py            Carga del catálogo semilla
    linking.py           Enlace de comidas existentes; aprendizaje de alias
    fdc.py               Importador de macros desde USDA FoodData Central
    seed/foods_mx.yaml   78 alimentos mexicanos + 12 recetas
  api/main.py            API de lectura (FastAPI)
  cli.py                 CLI
sql/views.sql            Vistas para Grafana
docs/                    Diseño completo del sistema (fases 1–5)
```

---

## Desarrollo

Para trabajar en el código conviene tener el intérprete en el host y dejar solo la
infraestructura en contenedores. Postgres expone el puerto 5432, así que los tests de
integración corren contra el mismo Postgres del stack.

```bash
docker compose up -d postgres
```

```bash
uv sync --all-groups
```

```bash
uv run alembic upgrade head
```

```bash
uv run pytest -q
```

```bash
uv run ruff check . && uv run ruff format --check .
```

Servidor con recarga automática:

```bash
uv run uvicorn glycomind.api.main:app --reload
```

Notas de plataforma:

- **Windows**: todo lo anterior funciona igual en PowerShell y en Git Bash. El CLI fuerza
  UTF-8 en su salida, así que los símbolos `Δ` y `≥` se imprimen bien aunque la consola
  esté en cp1252. En PowerShell, `uv` escribe su progreso a stderr y eso puede aparecer
  como error aunque el comando haya funcionado: mira el código de salida, no el color.
- El archivo `.env` es **opcional**. Solo hace falta si quieres cambiar puertos o
  credenciales; parte de `.env.example`. Las variables definidas en `docker-compose.yml`
  tienen prioridad dentro de los contenedores, así que un `.env` apuntando a `localhost`
  no los rompe.
- Los tests de `test_integration.py` necesitan PostgreSQL; se omiten solos si no está.

Tras cambiar código, la imagen se reconstruye con:

```bash
docker compose up -d --build
```

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
