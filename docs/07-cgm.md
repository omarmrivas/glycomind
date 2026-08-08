# 07 — Sensores CGM: comparación y recomendación para el MVP

## 1. Tabla comparativa

> **Corrección (verificada 8-ago-2026)**: en México **no se comercializa FreeStyle Libre 3**.
> El producto disponible es el **FreeStyle Libre 2 Plus**. Esta sección refleja esa realidad,
> que además **cambia un supuesto técnico importante**: ver §1.1.

| Criterio | **Dexcom G7 / G7 15-day / ONE+** | **FreeStyle Libre 2 Plus** ← disponible en MX | **Dexcom Stelo** (OTC) | **Abbott Lingo / Libre Rio** (OTC) | **Nightscout / xDrip+ / Juggluco** |
|---|---|---|---|---|---|
| **1. Acceso a datos** | ✅ El mejor: API oficial + export | ⚠️ Export CSV oficial; API no pública | ⚠️ Vía app/cuenta Dexcom | ⚠️ Vía app Abbott | ✅ Total (tú controlas el servidor) |
| **2. API oficial** | ✅ **Dexcom API v3** documentada, con sandbox | ❌ **No hay API pública de LibreView.** Existen integraciones por acuerdo (Epic, agregadores) y una API no oficial de LibreLinkUp obtenida por ingeniería inversa | Según programa Dexcom | ❌ | N/A (es tu backend) |
| **3. Export CSV** | ✅ (Clarity) | ✅ **Sí — LibreView → "Download glucose data"** | ✅ | Limitado | ✅ |
| **4. Export Excel** | Vía CSV | Vía CSV | Vía CSV | — | Vía CSV/JSON |
| **5. Integración con apps** | ✅ OAuth 2.0, tiers Sandbox→Limited→Full | ⚠️ Vía agregadores (Terra, Thryve) o CSV | Según programa | Limitado | ✅ |
| **6. BLE directo** | ⚠️ No documentado públicamente para terceros | ⚠️ Libre 2/2 Plus emite por BLE (alarmas y streaming a la app oficial); existen proyectos comunitarios, pero **fuera de EULA** | ❌ | ❌ | ✅ Ese es su propósito |
| **7. ¿Requiere app oficial?** | No para la API web | Sí para iniciar y leer el sensor | Sí | Sí | Depende del uploader |
| **8. Restricciones** | **Retraso de 1 h (servidores US) / 3 h (fuera de US y Japón)**; datos vía receptor USB sin retraso. Límite **60,000 llamadas/app/hora**. Scopes: EGV, calibraciones, eventos, dispositivo — **todo o nada**. Real-time (Partner Web APIs, autorizadas por FDA en jul-2021) es **por invitación** | 🔴 **Resolución de exportación de 15 min** (ver §1.1). EULA de LibreLinkUp prohíbe usos no autorizados | — | — | Ninguna técnica; sí de soporte |
| **9. Disponibilidad en México** | ✅ G7 disponible (Amazon MX, Mercado Libre) ⚠️ precio no verificado con fuente oficial | ✅ **Es el producto disponible**: Farmacias del Ahorro, Vitau, Diabetes Club, tienda oficial | ⚠️ **No verificado en MX** | ⚠️ **No verificado en MX** | N/A |
| **10. Facilidad de integración** | ★★★★★ (API) | ★★★☆☆ (CSV) | ★★☆☆☆ | ★★☆☆☆ | ★★★★☆ (requiere autohospedaje) |
| **11. Costo aprox. (MX)** | Mayor. ⚠️ Verificar con distribuidor | **~$1,629 MXN por sensor**, **15 días de uso** → **~$3,260 MXN/mes** (~US$95/mes) para cobertura continua | ~US$89–99 por 2 sensores (EE. UU.) | ~US$49–89 (EE. UU.) | Coste del sensor + servidor |
| **12. Precisión / resolución** | MARD **~8%** (G7 15-day); resolución **5 min** | MARD ⚠️ no verificado para 2 Plus específicamente (familia Libre ~8–9%). **Mide cada 1 min, almacena cada 15 min** | Basado en G7 | Basado en Libre | Igual al sensor subyacente |

### 1.1 🔴 El detalle que más afecta al diseño: resolución de 15 minutos

Abbott documenta que el sensor **mide la glucosa cada minuto pero almacena lecturas a
intervalos de 15 minutos** (8 h de histórico en memoria). El streaming de 1 min por Bluetooth
existe para la app en tiempo real, pero **lo que se exporta desde LibreView es el histórico
de 15 min** (`Record Type = 0`, "Historic Glucose").

Consecuencias directas, ya implementadas en el pipeline:

| Impacto | Magnitud | Cómo se maneja |
|---|---|---|
| **Subestimación del pico** | El apex real puede caer entre muestras. Con 15 min, la subestimación esperada es de varios mg/dL | `peak_underestimated` como bandera obligatoria cuando `native_resolution > 5 min`. El pico se reporta como **cota inferior**, no como valor |
| **Precisión del time-to-peak** | Granularidad **±15 min** | Coincide con el límite que ya imponía el retraso intersticial (5–15 min). **No es una pérdida real** |
| **iAUC** | Poco afectado: la regla trapezoidal sobre una curva postprandial suave con 8 puntos en 120 min es razonablemente exacta | Métrica primaria, sin cambios |
| **Regla de gaps** | ⚠️ Mi regla original (`max_gap > 20 min` ⇒ excluir) era **imposible de cumplir**: un solo punto perdido son 30 min | Regla **resolución-consciente**: `max_gap > max(20, 2.5 × resolución_nativa)` — con 15 min ⇒ 37.5 min, es decir más de 2 muestras consecutivas perdidas |
| **Ventana de basal** | `[-20, -5] min` puede contener **0 o 1 punto** | Ventana resolución-consciente: `max(20, 2 × resolución)` ⇒ 30 min con Libre 2 Plus, típicamente 2–3 puntos |
| **Cobertura** | Puntos esperados = `ventana / resolución + 1` | Se calcula contra la resolución **detectada empíricamente**, no asumida |

**La resolución nativa se detecta, no se asume**: se calcula como la mediana de las
diferencias entre lecturas consecutivas dentro de cada sesión de sensor. Así el mismo código
funciona con Libre 2 Plus (15 min), Dexcom (5 min) o cualquier otra fuente futura.

⚠️ **Sobre las cifras de MARD**: proceden de datos del fabricante y de estudios con
metodologías distintas. Un estudio *head-to-head* independiente reportó **11.4% (Libre 3)**
vs **18.5% (G7)** frente a glucosa capilar — muy alejado de las cifras de ficha técnica.
La heterogeneidad metodológica es alta. **Conclusión operativa: no uses el MARD para elegir;
ambos están en el mismo rango y ninguno es lo bastante preciso para que la diferencia importe
frente al ICC de 0.14–0.31 del propio fenómeno biológico.** Lo que sí importa: **no mezclar
marcas en el análisis** (CV del iAUC-2h de 3.7% intra-marca vs **12.5% entre marcas**).

---

## 2. Análisis de las tres vías de integración

### Vía A — API oficial de Dexcom ✅ *La correcta para producto*

```
Sandbox (automático)  →  Limited Access (≤5 usuarios, Data Licensing Agreement)
                      →  Full Access (revisión técnica y comercial + Strategic Partnerships)
```

**A favor**: documentada, con sandbox, OAuth 2.0, endpoints `/egvs`, `/events`,
`/calibrations`, `/devices`, `/dataRange`, `/alerts`. Es la única vía **inequívocamente legal
y sostenible** para un producto.

**En contra**:
- Retraso de **3 horas fuera de EE. UU.** — relevante para México. Irrelevante para el análisis
  postprandial (la ventana es de 3 h de todos modos), fatal para cualquier función "en vivo".
- **Scopes todo-o-nada**: no puedes pedir sólo glucosa.
- Acceso completo por aprobación, con tiempos no controlados por ti.
- La API real-time es **por invitación**; no cuentes con ella.

**Implicación de diseño**: `systemTime` para el análisis, `displayTime` sólo para mostrar;
polling cada 5 min como máximo (más frecuente no devuelve datos nuevos y consume presupuesto
de rate limit).

### Vía B — LibreView CSV ✅ *La correcta para el MVP*

El usuario descarga su CSV desde LibreView ("Glucose History" → "Download Glucose Data") y lo
sube. Es una función **oficial y soportada por Abbott**, sin EULA que se viole, sin ingeniería
inversa y sin dependencia de una API que puede romperse.

**A favor**: legal, estable, sin aprobaciones, funciona hoy, y Libre es lo que está disponible
y es asequible en México.

**En contra**: manual (fricción de UX), en lotes, con desfase de horas o días.

**Mitigación**: en un MVP de investigación con 1–10 usuarios motivados, subir un CSV una o dos
veces por semana es perfectamente aceptable. Un recordatorio automático lo resuelve.
El formato del CSV de LibreView está bien documentado por proyectos como Tidepool y Glooko;
escribir el parser es cuestión de horas.

### Vía C — LibreLinkUp / Juggluco ⚠️ *Sólo para tu propio dispositivo, nunca en producto*

La API de LibreLinkUp está documentada por la comunidad (Stoplight/GitHub) y hay clientes en
varios lenguajes. Juggluco lee Libre 3 por BLE directamente.

**Riesgos, en orden de gravedad**:
1. **Legal**: el EULA de LibreLinkUp restringe el uso a la aplicación autorizada. Construir un
   producto comercial encima es una exposición contractual real.
2. **Regulatorio**: acceder por vías no soportadas a datos de un dispositivo médico complica
   cualquier conversación futura con COFEPRIS o un socio clínico.
3. **Técnico**: Abbott puede cambiar el protocolo en cualquier momento y romperte el producto
   sin aviso.
4. **Juggluco requiere LibreLink instalado** y usa código del vendor — no es una
   reimplementación limpia.

**Uso defendible**: tú, con tu propio sensor, durante el desarrollo, para tener datos de 1 min
con los que construir y validar el pipeline. **No lo hagas parte del producto.**

### Vía D — Agregadores (Terra, Thryve/Spike) ⚠️ *Atajo con peajes*

Terra ofrece integración con Dexcom (con nivel gratuito que la incluye) y planes desde
**~US$399/mes** en el plan anual. Thryve ofrece un flujo OAuth sobre el ecosistema de Abbott.

**A favor**: una sola integración, normalización, menos trabajo de OAuth.
**En contra**: coste fijo alto para un MVP; **añade un tercero al flujo de datos de salud**
(con implicaciones de transferencia de datos bajo LFPDPPP y GDPR); y sigues dependiendo de los
límites de la API subyacente. Sensato en Fase 3+ si necesitas cubrir muchos fabricantes rápido;
prematuro antes.

---

## 3. Recomendación para el MVP de investigación/desarrollo

> **Usa FreeStyle Libre 2 Plus** (es lo disponible en México), **con ingesta por CSV de
> LibreView** como vía oficial, y construye desde el día 1 un `CGMAdapter` **resolución-consciente**
> con implementación paralela para la API v3 de Dexcom.

Razonamiento:

1. **Es lo que puedes comprar en México.** Libre 2 Plus está en Farmacias del Ahorro, Vitau y
   la tienda oficial. Libre 3 no se comercializa aquí. Para un proyecto que necesita **meses
   continuos** de uso, la disponibilidad manda sobre las especificaciones.
2. **15 días de uso por sensor** reduce el coste y —más importante— **reduce el número de
   transiciones entre sensores**, que son la mayor fuente de sesgo espurio. Un sensor de 15
   días genera la mitad de escalones que uno de 7 días. Esto es una ventaja analítica real.
3. **El CSV es legal, estable y suficiente.** El análisis es retrospectivo por naturaleza.
4. ⚠️ **Acepta la resolución de 15 min como restricción de diseño, no la combatas.** No
   intentes reconstruir la señal de 1 min por vías no oficiales: el coste legal y de
   fragilidad no compensa una precisión de pico que, de todos modos, queda dentro del margen
   del retraso intersticial.
5. **La abstracción `CGMAdapter` cuesta un día** y evita el acoplamiento a un fabricante.
   Implementa Dexcom v3 contra el **sandbox** en paralelo (gratuito y automático).
6. **Solicita acceso Limited de Dexcom pronto** aunque no lo uses: permite hasta 5 usuarios
   autorizados y la revisión toma tiempo. Empezarlo temprano es gratis.

**Presupuesto real de sensores** (dato de planificación, no técnico): a ~$1,629 MXN cada 15
días, **~$3,260 MXN/mes por usuario**. Para 12 meses de un solo usuario piloto son
**~$39,000 MXN**. Para 5 usuarios piloto durante 6 meses, ~$98,000 MXN. Es la partida de
gasto más grande de las fases 1–2, muy por encima del hardware de GPU.

**Lo que NO haría:**
- ❌ Construir sobre LibreLinkUp no oficial como base del producto.
- ❌ Pagar un agregador antes de tener usuarios.
- ❌ Elegir el sensor por MARD. La diferencia entre 8% y 9.4% es irrelevante frente a la
  variabilidad biológica intraindividual del fenómeno que quieres medir.
- ❌ Diseñar cualquier función que dependa de glucosa en tiempo real. Con 3 h de retraso fuera
  de EE. UU. en Dexcom y lotes en Libre, esa función no existe. Y una alerta en tiempo real
  sobre glucosa es, casi con seguridad, funcionalidad de dispositivo médico
  ([08-regulatorio.md](08-regulatorio.md)).
- ❌ Asumir la resolución en el código. **Detéctala.**

## 4. Requisitos que el adaptador debe cumplir sea cual sea la vía

```python
class AdapterCapabilities(BaseModel):
    vendor: str
    native_resolution_min: int  # 1 | 5 | 15
    data_delay_minutes: int  # 0 (CSV histórico) | 60 (Dexcom US) | 180 (Dexcom OUS)
    supports_backfill: bool
    max_history_days: int | None
    provides_trend: bool
    provides_calibration_events: bool
    provides_sensor_session_boundaries: bool  # crítico para excluir warm-up
    is_official_api: bool  # ← se registra en el linaje del dato
    legal_basis: str  # 'oauth_user_consent' | 'user_csv_upload' | ...
```

`is_official_api` y `legal_basis` van en el registro de linaje de cada lote de ingesta.
Si algún día hay una auditoría regulatoria o de privacidad, poder demostrar la base legal de
cada dato ingerido es la diferencia entre un trámite y un problema.
