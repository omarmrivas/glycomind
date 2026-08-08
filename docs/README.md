# GlycoMind — Diseño de arquitectura

Agente nutricionista personalizado que aprende la relación **individual** entre comidas y
respuesta glucémica, apoyado en evidencia científica trazable.

> **Estado**: documento de arquitectura (pre-implementación). Todas las afirmaciones sobre
> sensores, APIs, modelos y ciencia están verificadas contra fuentes primarias listadas en
> [REFERENCIAS.md](REFERENCIAS.md). Donde hay incertidumbre, está marcado explícitamente
> con ⚠️.

---

## Índice

| Doc | Contenido |
|---|---|
| [01-arquitectura.md](01-arquitectura.md) | Arquitectura de referencia, diagramas, flujos, eventos, seguridad, observabilidad |
| [02-nucleo-cientifico.md](02-nucleo-cientifico.md) | Métricas de glucosa, problemas reales de CGM, visión de alimentos, motor estadístico de personalización |
| [03-evidencia-y-research-agent.md](03-evidencia-y-research-agent.md) | Base de conocimiento científico, RAG, grafo de claims, evidence grading, conflictos, Research Agent, microbiota |
| [04-recomendaciones.md](04-recomendaciones.md) | Recommendation Engine y mecanismo de explicación |
| [05-modelo-de-datos.md](05-modelo-de-datos.md) | Modelo de datos completo y qué vive en cada almacén |
| [06-stack-modelos-infra.md](06-stack-modelos-infra.md) | Stack, comparación Qwen vs. modelos especializados, hardware, app móvil |
| [07-cgm.md](07-cgm.md) | Comparativa de sensores CGM y recomendación para MVP |
| [08-regulatorio.md](08-regulatorio.md) | Límites médicos, privacidad, LFPDPPP 2025, GDPR, COFEPRIS/SaMD |
| [09-roadmap-riesgos.md](09-roadmap-riesgos.md) | MVP por fases, roadmap 3/6/12 meses, riesgos técnicos/científicos/regulatorios |
| [REFERENCIAS.md](REFERENCIAS.md) | Todas las fuentes |

---

## Resumen ejecutivo

### El hallazgo que debe gobernar todo el diseño

Antes de hablar de arquitectura hay que internalizar un resultado que **contradice
parcialmente la premisa del producto**:

> Hall *et al.* midieron 1,189 respuestas glucémicas a **comidas duplicadas** (la misma comida,
> a la misma persona, con ~1 semana de diferencia, en régimen intrahospitalario controlado) en
> 30 adultos sin diabetes. La fiabilidad intraindividual del iAUC fue **ICC = 0.31 (Abbott
> Libre Pro)** y **ICC = 0.14 (Dexcom G4)**. La variabilidad de la respuesta a comidas
> *duplicadas* fue **similar** a la variabilidad entre comidas *distintas*.
> — [AJCN 2024 / medRxiv 2023](https://pubmed.ncbi.nlm.nih.gov/37503002/)

Traducción operativa: **una sola medición de "arroz → +58 mg/dL" no es información. Es ruido.**
Un sistema que muestre esa cifra como un hallazgo personal está mintiendo con confianza
estadística cero.

Esto no invalida el proyecto — Zeevi *et al.* (Cell 2015, n=800) sí demostraron que existe
variabilidad interpersonal real y predecible — pero impone tres requisitos no negociables:

1. **Nada de claims con n=1.** Toda afirmación personal exige **exposiciones repetidas** y un
   intervalo de credibilidad que excluya el efecto trivial. El sistema debe poder decir
   *"todavía no sé"* y decirlo a menudo.
2. **El agregado es la unidad de análisis, no la comida.** Se modela la distribución de la
   respuesta a un alimento/combinación, no eventos individuales.
3. **Si quieres causalidad, hay que aleatorizar.** El sistema debe incluir un motor de
   **ensayos N-of-1** que proponga replicaciones deliberadas. Es la diferencia entre una app de
   *logging* y un instrumento.

### Las 8 decisiones de arquitectura

| # | Decisión | Por qué |
|---|---|---|
| 1 | **Rediseñar la arquitectura multi-agente propuesta.** Sólo 2 agentes LLM reales: el conversacional y el Research Agent. "Glucose Analysis Agent", "Nutrition Agent" y "Recommendation Engine" son **servicios deterministas** expuestos como *tools*. | Calcular iAUC o ajustar un modelo jerárquico con un LLM es introducir no-determinismo en el único lugar donde el sistema debe ser auditable y reproducible. |
| 2 | **Núcleo estadístico = modelo bayesiano jerárquico con partial pooling**, no reglas ni ML genérico. | Es la respuesta matemáticamente correcta a "Usuario A ≠ Usuario B con n pequeño": encoge hacia la media poblacional cuando hay pocos datos y libera al individuo cuando hay muchos, y entrega incertidumbre nativa. |
| 3 | **Todo claim (personal o científico) es un objeto de primera clase** con grado de evidencia, procedencia, versión y aristas `supports` / `contradicts`. | Requisito directo del punto 5 del brief: representar conflictos en vez de sobrescribir. |
| 4 | **Un solo PostgreSQL** (+ `pgvector` + TimescaleDB opcional) + object storage. Sin Neo4j, sin Qdrant, sin Kafka en el MVP. | 288 lecturas/día/usuario = 105k filas/año/usuario. Postgres se ríe de eso. El grafo tiene ~10⁴ nodos: es una tabla de aristas. |
| 5 | **Qwen3.6-27B (Apache 2.0, multimodal, 262k ctx) como caballo de batalla local**, con *model router* y un *eval harness* propio. | Ya es multimodal nativo → un modelo menos que operar. ⚠️ Qwen3.8-27B open-weights fue anunciado para la semana del 10-ago-2026 pero **no está publicado** al 8-ago-2026: no construyas dependiendo de él. |
| 6 | **La visión estima, el usuario confirma.** El modelo multimodal nunca escribe gramos directamente en la base de datos sin *loop* de confirmación. | Los mejores MLLM comerciales tienen **MAPE 35–37% en peso y energía** sobre fotos estandarizadas. Un ±36% en carbohidratos destruye cualquier inferencia posterior. |
| 7 | **MVP con FreeStyle Libre 2 Plus + import CSV oficial de LibreView**; adaptador Dexcom API como camino productizable. El pipeline es **resolución-consciente**. | Libre 2 Plus es *lo que se vende en México* (~$1,629 MXN, 15 días); Libre 3 no. El CSV de LibreView es una vía **oficial y legal**. ⚠️ Restricción clave: el sensor mide cada minuto pero **almacena y exporta cada 15 min** — todos los umbrales de QC se derivan de la resolución **detectada**, nunca asumida. Ver [07-cgm.md §1.1](07-cgm.md). |
| 8 | **Posicionamiento "bienestar general", no dispositivo médico.** Sin dosificación de insulina, sin diagnóstico, sin objetivos terapéuticos. | En cuanto calculas carbohidratos *para dosificar insulina*, eres un SaMD clase II/III en cualquier jurisdicción. Ese es el límite duro del proyecto. |

### Qué construiría primero

**Un pipeline determinista de emparejamiento comida↔curva, con un buen visor, y cero LLM en la ruta crítica.**

Concretamente, las primeras 4 semanas:

1. Ingesta de CSV de LibreView → normalización → `glucose_reading` en Postgres.
2. Registro de comidas (foto + texto + hora) por un bot de Telegram/WhatsApp. Sin app.
3. **Meal-window pairing engine**: dada una comida en `t`, extraer la ventana `[t-30min, t+180min]`,
   validar calidad (cobertura, gaps, comidas solapadas, ejercicio), calcular métricas y guardar
   `meal_glucose_response` con una bandera de calidad.
4. Un dashboard de Grafana / Streamlit sobre esas tablas.

Si eso funciona con datos reales tuyos durante 3–4 semanas, tienes el activo del proyecto.
Todo lo demás — RAG, research agent, microbiota, recomendaciones — se apoya en esta tabla.
Si esa tabla está sucia, nada de lo de arriba vale nada.

Detalle completo en [09-roadmap-riesgos.md](09-roadmap-riesgos.md).

---

## Crítica a la arquitectura propuesta en el brief

La propuesta original:

```
Agent Orchestrator
    ├── Nutrition Agent
    ├── Glucose Analysis Agent
    ├── Food Vision Agent
    ├── Research Agent
    ├── Microbiome Knowledge Agent
    └── Recommendation Engine
```

Cuatro problemas:

1. **Sobre-agentifica.** El "Glucose Analysis Agent" no necesita razonamiento libre: necesita
   una implementación correcta y versionada de la regla trapezoidal y un control de calidad de
   señal. Un LLM ahí es un generador de números plausibles no reproducibles.
2. **"Microbiome Knowledge Agent" no es un agente, es un *namespace*.** Es un subconjunto de
   la base de conocimiento con un *evidence grading* más severo. Darle un agente propio invita
   a que el sistema produzca afirmaciones de microbiota sin el rigor del resto.
3. **Falta la capa que realmente decide si el sistema puede hablar.** No hay ningún componente
   que responda "¿tengo suficiente evidencia para decir esto?". Ese es el componente más
   importante del producto: lo llamo **Evidence Sufficiency Gate**.
4. **Faltan los generadores de datos causales.** Sin un motor de experimentos N-of-1, el sistema
   sólo puede producir correlaciones observacionales confundidas por hora del día, actividad,
   sueño y estrés.

La arquitectura corregida está en [01-arquitectura.md](01-arquitectura.md).
