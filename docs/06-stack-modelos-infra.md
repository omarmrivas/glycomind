# 06 — Stack tecnológico, modelos, infraestructura y app móvil

## 1. Evaluación de la decisión "Qwen3.6 / Qwen3.8"

### 1.1 Estado real de la familia Qwen (verificado a 8-ago-2026)

| Modelo | Fecha | Pesos abiertos | Notas |
|---|---|---|---|
| **Qwen3.6-27B** | 22-abr-2026 | ✅ **Apache 2.0** | Denso 27B; atención híbrida (Gated DeltaNet + Gated Attention); **262,144 tokens nativos**, extensible a ~1M con YaRN; **multimodal: acepta imagen y vídeo**; modo *thinking* por defecto |
| **Qwen3.6-35B-A3B** | 16-abr-2026 | ✅ Apache 2.0 | MoE ~35B totales / ~3B activos — mucho más barato por token |
| **Qwen3.7-Max / Plus** | may–jun 2026 | ❌ **Solo API** | Sin pesos abiertos |
| **Qwen3.8-Max** | 2-ago-2026 | ❌ Solo API | 2.4T params, 95B activos, 1M ctx, multimodal nativo |
| **Qwen3.8-27B** | anunciado | ⚠️ **Prometido para la semana del 10-ago-2026 — no publicado al 8-ago-2026** | Sin ficha técnica oficial: arquitectura, contexto, licencia y benchmarks **no confirmados** |

### 1.2 Veredicto

**Qwen3.6-27B es una elección correcta y la recomiendo como modelo principal**, por cuatro
razones concretas y no por popularidad:

1. **Apache 2.0** — sin ambigüedad legal para un producto que maneja datos de salud.
2. **Multimodal nativo** — elimina un modelo entero del stack. No necesitas un VLM separado
   para las fotos de comida.
3. **262k de contexto** — un paper completo con tablas cabe sin fragmentar.
4. **Cabe en una GPU de consumo** — ~17 GB en Q4_K_M / AWQ-Int4, es decir una RTX 4090 o
   3090 de 24 GB.

**Advertencias importantes:**

- ⚠️ **No planifiques asumiendo Qwen3.8-27B.** Al día de hoy es un compromiso con fecha, no
  una descarga. Si aparece y mejora, migrar debe ser cambiar una línea de configuración —
  por eso el **model router** y el **eval harness** son parte del diseño, no un extra.
- ⚠️ **Qwen3.7 y 3.8-Max son API-only.** Si tu requisito es ejecución local (y con datos de
  salud debería serlo), están fuera. Úsalos, como mucho, como *juez* offline en el eval
  harness con datos sintéticos.
- ⚠️ **Modo *thinking* por defecto**: excelente para extracción científica, caro y lento para
  clasificación trivial. Desactívalo (`enable_thinking: False`) en las tareas rutinarias.

### 1.3 La decisión más importante no es qué modelo, sino no usar uno solo

Esta es la respuesta directa al punto 8 del brief.

| Tarea | Modelo recomendado | Por qué **no** el LLM principal |
|---|---|---|
| **Agente conversacional** | Qwen3.6-27B (thinking off) o **35B-A3B** para más throughput | — |
| **Razonamiento / síntesis científica** | Qwen3.6-27B (thinking **on**), contexto largo | Es donde el razonamiento paga |
| **Análisis de artículos (extracción PICO)** | Qwen3.6-27B con salida forzada por gramática (`guided_json` en vLLM) | La restricción por esquema importa más que el tamaño |
| **Clasificación** (tipo de estudio, intención, sentimiento del feedback) | **Modelo pequeño fine-tuneado** (ModernBERT / Qwen3-0.6B) o Qwen3.6 con logit bias | 100–1000× más barato, más rápido, y **determinista**. Un LLM de 27B para clasificar en 8 clases es un desperdicio |
| **Embeddings** | **Qwen3-Embedding** (líder MTEB v2 abierto, ~119 idiomas) o **BGE-M3** (denso+sparse en un modelo, excelente multilingüe híbrido) | Modelos generativos no producen buenos embeddings |
| **Reranking** | `bge-reranker-v2-m3` o Qwen3-Reranker | Cross-encoder pequeño supera a un LLM grande en ranking, a 1/100 del coste |
| **Visión (comida)** | Qwen3.6-27B multimodal | ✅ El mismo modelo. Ventaja real |
| **OCR** | **PaddleOCR / Tesseract**, no un VLM | Los VLM alucinan texto. Un OCR clásico se equivoca de forma predecible y auditable. VLM sólo como fallback para tablas complejas |
| **Layout de PDF científico** | `docling` / `marker` / Nougat | Modelos especializados en estructura de documento |
| **Series temporales de glucosa** | ❌ **Ningún LLM.** SciPy/NumPy + reglas + NumPyro | Un LLM calculando un iAUC es un generador de números plausibles. Esto es aritmética y debe ser aritmética |
| **Modelo de personalización** | **NumPyro (NUTS)** o PyMC | Estadística bayesiana, no lenguaje |
| **Detección de anomalías** | Reglas + `sktime`/`ruptures` | Determinista, explicable |
| **NLI (verificar que la cita dice lo que se afirma)** | Cross-encoder NLI pequeño (DeBERTa-v3-NLI o similar) | Tarea estrecha, modelo estrecho |

### 1.4 Cuándo usar LLM vs. otra cosa (regla práctica)

```
¿La salida debe ser reproducible bit a bit?           → código determinista
¿Es aritmética o estadística?                          → NumPy/SciPy/NumPyro
¿Es una decisión de seguridad clínica?                 → reglas + revisión humana
¿Es una consulta sobre datos estructurados?            → SQL
¿Es una clasificación con clases fijas y datos?        → modelo pequeño fine-tuneado
¿Es extracción de estructura desde texto libre?        → LLM + JSON Schema forzado
¿Es lenguaje natural entrando o saliendo?              → LLM
¿Es conocimiento del mundo que cambia?                 → herramienta externa + RAG, nunca los pesos
```

Corolario, incómodo pero cierto: **en este sistema el LLM hace menos del 20% del trabajo.**
Eso es señal de buen diseño, no de falta de ambición.

---

## 2. Stack recomendado

| Capa | Recomendación | Justificación |
|---|---|---|
| **Backend** | **Python 3.12 + FastAPI**, monolito modular | Es donde vive todo el ecosistema científico (NumPyro, ArviZ, Polars, scikit-learn). Tu experiencia en .NET es valiosa, pero **partir el stack entre .NET y Python por un sistema con núcleo estadístico sería un error** — el coste de cruzar la frontera supera cualquier ganancia. Si quieres .NET, úsalo para un panel administrativo aparte, no para el núcleo |
| **Validación / tipos** | Pydantic v2 en todas las fronteras | Los contratos de procedencia e incertidumbre sólo funcionan si están tipados |
| **Workers** | `arq` (Redis) o Celery | `arq` es más simple y async-nativo |
| **Agent framework** | **Pydantic AI** para el agente conversacional; **LangGraph** sólo para el Research Agent | Pydantic AI: type-safety y salida estructurada, encaja con FastAPI/Pydantic. LangGraph: el Research Agent es una máquina de estados con ciclos y ejecución duradera (reanudar tras caída) — ahí sí paga su complejidad. **No uses un framework pesado para el chat** |
| **Serving LLM** | **vLLM** (producción) + Ollama (desarrollo local) | vLLM: continuous batching, tensor parallelism, `guided_json`. Ollama es cómodo pero no da throughput multiusuario |
| **Router de modelos** | LiteLLM o un router propio delgado | Cambiar de Qwen3.6 a 3.8 debe ser configuración |
| **LLM principal** | Qwen3.6-27B AWQ/GPTQ-Int4 | §1 |
| **Visión** | El mismo Qwen3.6-27B | §1 |
| **Embeddings** | Qwen3-Embedding (0.6B/4B) o BGE-M3 | BGE-M3 si quieres denso+sparse en un modelo |
| **Reranker** | bge-reranker-v2-m3 | |
| **Base de datos** | **PostgreSQL 17** + `pgvector` + `pg_trgm` + (TimescaleDB cuando toque) | Un solo almacén |
| **Vector DB** | `pgvector` ahora; Qdrant si se cruza el umbral | |
| **Knowledge graph** | Tablas en Postgres; Memgraph/Neo4j si se cruza el umbral | |
| **Time-series** | Postgres + BRIN → TimescaleDB (extensión, migración trivial) | |
| **Object storage** | MinIO (self-hosted) → S3/R2 | |
| **Broker** | Redis + `arq`; NATS JetStream en Fase 4+ | |
| **Estadística** | **NumPyro** (JAX) + ArviZ; Polars para ETL | NumPyro es mucho más rápido que PyMC para NUTS |
| **Observabilidad** | OpenTelemetry → Grafana + Loki + Tempo + Prometheus | Ya lo conoces |
| **LLM observability** | **Langfuse** (self-hosted) | Trazas, coste, evals; self-hosted por privacidad |
| **Auth** | Keycloak o Authentik (self-hosted), OIDC + passkeys | No entregar identidades de salud a un SaaS |
| **Mobile** | **Flutter** (Fase 2+) | §4 |
| **Fase 1 UI** | Bot de Telegram + PWA con Streamlit/HTMX | La foto y el texto entran por Telegram sin construir una app |
| **Despliegue** | Docker Compose (Fase 1–2) → k3s/k8s (Fase 4+) | Compose es suficiente para un solo host |
| **CI/CD** | GitHub Actions; migraciones con Alembic | |
| **Testing** | pytest + Hypothesis (property-based para las métricas) + **datasets de regresión de glucosa** | Las métricas deben tener tests de oráculo con curvas sintéticas de iAUC conocido |

### Lo que deliberadamente **no** recomiendo

- **LangChain "clásico"** para el chat: capa de abstracción con coste de depuración alto.
- **Un microservicio por agente** en fase temprana.
- **Kafka** antes de tener consumidores múltiples reales.
- **Fine-tuning del LLM principal** antes de la Fase 5. Casi todo lo que parece requerir
  fine-tuning se resuelve con salida estructurada + RAG + un clasificador pequeño.
- **Un "vector store" para los datos del usuario.** Los datos glucémicos son numéricos y
  relacionales; meterlos en un índice vectorial para que el chat "los recuerde" es un
  antipatrón que garantiza cifras inventadas. El agente consulta con SQL vía tools.

---

## 3. Infraestructura para ejecución local: estimación

### 3.1 Requisitos por componente

| Componente | VRAM | Notas |
|---|---|---|
| Qwen3.6-27B @ AWQ/GPTQ-Int4 | **~17 GB** pesos + KV cache | Reportado: cabe en 24 GB con margen; **~72 tok/s en una RTX 3090 con vLLM** |
| Qwen3.6-27B @ Q8 | ~30 GB | Requiere 32 GB+ |
| Qwen3.6-27B @ BF16 | ~54 GB+ | 2 GPUs o A100/H100 |
| Qwen3.6-35B-A3B (MoE) @ Int4 | ~20–22 GB | Mucho más rápido por token (3B activos); buena opción para el chat |
| Qwen3-Embedding-0.6B | ~1.5 GB | Puede correr en CPU si hace falta |
| bge-reranker-v2-m3 | ~1.2 GB | |
| NumPyro (MCMC) | 0 GB VRAM (CPU) o GPU opcional | Con < 10⁵ observaciones, CPU multinúcleo es suficiente |

**Nota sobre el KV cache**: con contexto largo (papers de 100k+ tokens) el KV cache puede
superar el tamaño de los pesos. Con 24 GB y el modelo en Int4, el contexto práctico ronda
32–64k tokens. Para procesar papers completos, limita el contexto por petición o usa
cuantización de KV cache (FP8).

### 3.2 Tres configuraciones

| | **Dev / MVP (1–5 usuarios)** | **Piloto (10–50 usuarios)** | **Producción pequeña (100–500)** |
|---|---|---|---|
| GPU | 1× RTX 4090 24 GB (o 3090 24 GB) | 1× RTX 6000 Ada / L40S 48 GB | 2× L40S 48 GB, o A100 80 GB |
| CPU | 12–16 núcleos | 24–32 núcleos | 32–64 núcleos |
| RAM | 64 GB | 128 GB | 256 GB |
| Disco | 2 TB NVMe | 4 TB NVMe + backup | NVMe + object storage separado |
| Coste aprox. hardware | ~US$3–4.5k | ~US$12–18k | ~US$35–60k |
| Alternativa cloud | RunPod/Vast.ai bajo demanda | GPU dedicada mensual | Reservada |
| Qué corre | Todo en un host, Docker Compose | Inferencia separada del resto | k3s, pools de inferencia separados |

**Recomendación para empezar**: **1× RTX 4090 24 GB, 64 GB RAM, 2 TB NVMe.** Con Qwen3.6-27B
en Int4 sirve chat, visión y extracción científica para el desarrollo y los primeros usuarios.

⚠️ **Advertencia de coste oculto**: la carga real no es el chat (unas pocas peticiones al día
por usuario). Es la **ingesta científica en batch** — procesar 500 papers con modo *thinking*
activado son horas de GPU. Planifica el batch nocturno con presupuesto de tokens explícito y
usa el MoE (35B-A3B) para el pre-cribado, reservando el denso 27B para la extracción final.

### 3.3 Estrategia híbrida (pragmática)

Nada obliga a que todo sea local:

- **Datos de usuario (glucosa, fotos, comidas) → siempre local.** Es el requisito de privacidad.
- **Papers científicos → son públicos.** Procesarlos en una API comercial no expone nada del
  usuario y puede ser mucho más barato que amortizar hardware. Es una decisión de coste, no
  de privacidad.
- Esa separación la impone la arquitectura: el Research Agent nunca ve datos de usuario
  ([01-arquitectura.md §7](01-arquitectura.md)), así que puede usar un modelo remoto sin
  comprometer nada.

---

## 4. Aplicación móvil

### 4.1 ¿Debería existir? Sí, pero no primero

Funciones que **exigen** móvil: cámara en el momento de comer, notificaciones push
(recordatorio de registrar, aviso de ventana completada), lectura NFC/BLE del sensor,
integración con HealthKit / Health Connect.

Funciones que **no** la exigen: dashboards, chat, revisión histórica, importación de CSV.

**Por eso la Fase 1 no lleva app.** Un bot de Telegram/WhatsApp cubre foto + texto + hora + chat
con cero coste de desarrollo móvil, cero fricción de instalación y cero ciclo de review de
tiendas. Es la forma más rápida de llegar a datos reales, que es lo único que importa al
principio.

### 4.2 Comparación

| Criterio | **Flutter** | React Native | Kotlin nativo | Swift nativo | PWA |
|---|---|---|---|---|---|
| Velocidad de desarrollo (1 equipo, 2 plataformas) | ★★★★★ | ★★★★☆ | ★★☆☆☆ | ★★☆☆☆ | ★★★★★ |
| Cámara + procesamiento de imagen | ★★★★☆ | ★★★★☆ | ★★★★★ | ★★★★★ | ★★☆☆☆ |
| **HealthKit / Health Connect** | ★★★★☆ (`health`: envuelve ambos, soporta `BLOOD_GLUCOSE`) | ★★★☆☆ | ★★★★★ | ★★★★★ | ❌ **No accesible** |
| **BLE directo al sensor** | ★★★☆☆ (`flutter_blue_plus`) | ★★★☆☆ | ★★★★★ | ★★★★☆ | ❌ (Web Bluetooth no en iOS) |
| NFC (Libre) | ★★★☆☆ | ★★☆☆☆ | ★★★★★ | ★★★★☆ | ❌ |
| Notificaciones push | ★★★★★ | ★★★★★ | ★★★★★ | ★★★★★ | ★★☆☆☆ (limitado en iOS) |
| Gráficas de series densas | ★★★★★ (renderiza en canvas propio) | ★★★☆☆ | ★★★★★ | ★★★★★ | ★★★★☆ |
| Offline-first | ★★★★★ | ★★★★☆ | ★★★★★ | ★★★★★ | ★★★☆☆ |
| Coste de mantener 2 plataformas | Bajo | Bajo | Alto (×2) | Alto (×2) | Muy bajo |

### 4.3 Recomendación: **Flutter**

Razones:
1. **Una base de código**, crítico para un equipo pequeño.
2. El paquete `health` envuelve **HealthKit (iOS) y Health Connect (Android)** con soporte de
   `BLOOD_GLUCOSE`. ⚠️ Nota: Google Fit está deprecado y fue retirado del paquete en la v11 —
   el camino en Android es **Health Connect**, y requiere que el usuario lo tenga instalado.
3. Renderiza sus propios gráficos: las curvas de glucosa con 288 puntos/día se comportan mejor
   que en React Native.
4. Offline-first sólido (Drift/SQLite) — necesario porque se registra comida sin cobertura.

**Cuándo elegiría otra cosa:** si acabases necesitando **BLE directo al sensor** como función
central, Kotlin nativo para Android primero es defendible — es donde vive el ecosistema
(Juggluco, xDrip+) y donde la ingeniería inversa de Libre es viable. Pero ver
[07-cgm.md](07-cgm.md) sobre los riesgos legales de esa vía.

**PWA**: útil para los dashboards y para el piloto interno, insuficiente como producto —
sin HealthKit, sin BLE, sin NFC y con push limitado en iOS.

---

## 5. Contrato de evaluación de modelos (obligatorio antes de cambiar de modelo)

Cambiar de Qwen3.6 a Qwen3.8 (o a cualquier otro) sin un eval harness es apostar. Suites
mínimas, ejecutadas en CI:

1. **Visión de alimentos** — 200 fotos etiquetadas con pesos reales (constrúyelo tú con una
   báscula; 2 semanas de trabajo, es el activo más valioso del proyecto después de los datos
   glucémicos). Métrica: MAPE de peso y de carbohidratos, tasa de items no detectados,
   tasa de items alucinados. Línea base a batir: **~36% MAPE**.
2. **Extracción científica** — 50 papers anotados a mano con `design`, `n`, `population`,
   dirección del efecto. Métrica: exactitud por campo; **exactitud en `design` y `species`
   debe ser > 0.95** (son los campos que gobiernan las barreras de seguridad).
3. **Fidelidad de citación** — dado un bundle, ¿el texto generado contiene alguna cifra que no
   esté en el bundle? Objetivo: **0**.
4. **Cumplimiento de hedging** — ¿usó el verbo causal con un diseño observacional? Objetivo: **0**.
5. **Rechazo de fuera de alcance** — 100 prompts que piden dosis de insulina, diagnóstico o
   interpretación de síntomas. Objetivo: **100% de derivación correcta**.
6. **Resistencia a prompt injection** — PDFs y páginas web con instrucciones embebidas.
   Objetivo: **0 acciones ejecutadas**.

Las suites 3–6 son **de seguridad**: una regresión ahí bloquea el despliegue, sin excepción.
