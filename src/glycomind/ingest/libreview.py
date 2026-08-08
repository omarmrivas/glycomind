"""Parser del CSV de exportacion de LibreView (Abbott FreeStyle Libre).

Contexto verificado (ver docs/07-cgm.md y docs/REFERENCIAS.md):

* La exportacion es **manual y protegida por reCAPTCHA** ("Confirme que no es un robot").
  No existe forma legitima de automatizarla. Todo el diseno asume lotes manuales con
  solapes: el parser es idempotente y reporta que hay de nuevo.
* Los timestamps son **hora de pared local sin zona horaria**. Hay que pasarla explicita.
* El **orden de fecha es ambiguo** (DD-MM vs MM-DD) y NO se puede deducir del idioma: el
  propio articulo de soporte en espanol usa un ejemplo mes-primero. Se autodetecta.
* Abbott advierte que **volver a guardar el archivo** (p.ej. abrirlo en Excel) cambia el
  formato y rompe la importacion. Se detecta y se avisa.
* El CSV incluye carbohidratos y notas registrados en la app. Glooko los descarta;
  nosotros los aprovechamos como registro de comidas de friccion cero.

El parseo es **dirigido por columnas, no por el codigo numerico de Record Type**: la
numeracion varia entre versiones y locales, mientras que las columnas son estables.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

from glycomind.domain.models import RawFoodEntry, RawReading

MMOL_TO_MGDL = 18.0182
VENDOR = "abbott"

# Un CSV de LibreView tiene una o dos lineas de preambulo antes de la cabecera real.
_MAX_PREAMBLE_LINES = 8


class LibreViewParseError(ValueError):
    """El archivo no es un export de LibreView utilizable."""


def _norm(s: str) -> str:
    """Normaliza para comparar cabeceras entre locales: sin acentos, minusculas, compacto."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


@dataclass(frozen=True, slots=True)
class ColumnMap:
    timestamp: int
    serial: int | None
    device: int | None
    historic: int | None
    scan: int | None
    strip: int | None
    carbs_g: int | None
    carbs_servings: int | None
    notes: int | None
    glucose_unit: str  # 'mg/dL' | 'mmol/L'


def _find_col(
    headers: Sequence[str], *, must: Sequence[str], forbid: Sequence[str] = ()
) -> int | None:
    """Primera columna cuyo nombre normalizado contiene todos los ``must`` y ningun ``forbid``."""
    for i, h in enumerate(headers):
        n = _norm(h)
        if all(m in n for m in must) and not any(f in n for f in forbid):
            return i
    return None


def _find_any(headers: Sequence[str], alternatives: Sequence[dict]) -> int | None:
    for alt in alternatives:
        idx = _find_col(headers, must=alt["must"], forbid=alt.get("forbid", ()))
        if idx is not None:
            return idx
    return None


def _build_column_map(headers: Sequence[str]) -> ColumnMap:
    ts = _find_any(
        headers,
        [
            {"must": ["timestamp"]},
            {"must": ["sello", "tiempo"]},
            {"must": ["marca", "tiempo"]},
            {"must": ["hora", "dispositivo"]},
            {"must": ["fecha"], "forbid": ["nacimiento"]},
        ],
    )
    if ts is None:
        raise LibreViewParseError(
            "No encuentro la columna de timestamp. ¿Es realmente un export de LibreView "
            "('Historial de glucosa' -> 'Descargar datos de glucosa')?"
        )

    historic = _find_any(
        headers,
        [
            {"must": ["historic", "glucos"]},
            {"must": ["historico", "glucos"]},
            {"must": ["glucosa", "historic"]},
        ],
    )
    scan = _find_any(
        headers,
        [
            {"must": ["scan", "glucos"]},
            {"must": ["escane", "glucos"]},
            {"must": ["glucosa", "escane"]},
        ],
    )
    strip = _find_any(
        headers,
        [{"must": ["strip", "glucos"]}, {"must": ["tira", "glucos"]}],
    )

    unit = "mg/dL"
    for idx in (historic, scan, strip):
        if idx is None:
            continue
        n = _norm(headers[idx])
        if "mmol" in n:
            unit = "mmol/L"
            break
        if "mg/dl" in n or "mg/dl" in n.replace(" ", ""):
            unit = "mg/dL"
            break

    return ColumnMap(
        timestamp=ts,
        serial=_find_any(headers, [{"must": ["serial"]}, {"must": ["serie"]}]),
        device=_find_any(
            headers,
            [
                {"must": ["device"], "forbid": ["timestamp"]},
                {"must": ["dispositivo"], "forbid": ["sello", "hora"]},
            ],
        ),
        historic=historic,
        scan=scan,
        strip=strip,
        carbs_g=_find_any(
            headers,
            [{"must": ["carbohydrate", "gram"]}, {"must": ["carbohidrato", "gramo"]}],
        ),
        carbs_servings=_find_any(
            headers,
            [{"must": ["carbohydrate", "serving"]}, {"must": ["carbohidrato", "racion"]}],
        ),
        notes=_find_any(headers, [{"must": ["notes"]}, {"must": ["notas"]}]),
        glucose_unit=unit,
    )


# --------------------------------------------------------------------------------------
# Fechas
# --------------------------------------------------------------------------------------

_DATE_RE = re.compile(
    r"^\s*(\d{1,4})[-/.](\d{1,2})[-/.](\d{2,4})[\sT]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|a\.?\s?m\.?|p\.?\s?m\.?)?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _DateParts:
    """Componentes crudos, SIN interpretar cual es dia, mes o ano.

    La interpretacion se difiere a ``_assemble`` porque depende del orden detectado para
    el archivo completo, no de la fila.
    """

    g1: int
    g2: int
    g3: int
    hour: int
    minute: int
    second: int
    meridiem: str | None


def _split_datetime(raw: str) -> _DateParts | None:
    m = _DATE_RE.match(raw)
    if not m:
        return None
    g1, g2, g3, hh, mm, ss, mer = m.groups()
    meridiem = None
    if mer:
        n = _norm(mer).replace(".", "").replace(" ", "")
        meridiem = "pm" if n.startswith("p") else "am"
    return _DateParts(
        g1=int(g1),
        g2=int(g2),
        g3=int(g3),
        hour=int(hh),
        minute=int(mm),
        second=int(ss or 0),
        meridiem=meridiem,
    )


def _apply_meridiem(hour: int, meridiem: str | None) -> int:
    if meridiem is None:
        return hour
    if meridiem == "pm" and hour < 12:
        return hour + 12
    if meridiem == "am" and hour == 12:
        return 0
    return hour


def _normalize_year(y: int) -> int:
    return y + 2000 if y < 100 else y


def _assemble(parts: _DateParts, order: str) -> datetime | None:
    """Construye el datetime naive segun el orden. ``order``: 'DMY' | 'MDY' | 'YMD'."""
    if order == "YMD":
        year, month, day = parts.g1, parts.g2, parts.g3
    elif order == "DMY":
        day, month, year = parts.g1, parts.g2, _normalize_year(parts.g3)
    elif order == "MDY":
        month, day, year = parts.g1, parts.g2, _normalize_year(parts.g3)
    else:
        raise ValueError(f"orden de fecha desconocido: {order}")
    try:
        return datetime(
            year,
            month,
            day,
            _apply_meridiem(parts.hour, parts.meridiem),
            parts.minute,
            parts.second,
        )
    except ValueError:
        return None


def detect_date_order(
    raw_timestamps: Sequence[str], hint: str | None = None
) -> tuple[str, list[str]]:
    """Determina si las fechas son DD-MM, MM-DD o YYYY-MM-DD.

    Estrategia en tres niveles, de mas fuerte a mas debil:

    1. **Decisivo**: si algun primer componente es > 12, es dia (DMY); si algun segundo
       componente es > 12, es dia (MDY). Si ambos ocurren, el archivo es incoherente.
       En un export real de 14 dias esta regla resuelve practicamente siempre.
    2. **Densidad temporal**: una serie de CGM es densa y regular. Interpretar
       ``01-08``, ``02-08``, ``03-08`` como MDY convierte 3 dias en 3 meses, y la
       densidad de puntos se desploma dos ordenes de magnitud. Ese contraste es un
       discriminador fuerte; la monotonia, en cambio, no discrimina (ambas lecturas son
       monotonas).
    3. **Hint explicito** del usuario; si tampoco lo hay, se falla con mensaje claro en
       vez de adivinar. Adivinar mal desplaza comidas hasta 11 meses.
    """
    warnings: list[str] = []
    parsed = [p for p in (_split_datetime(t) for t in raw_timestamps) if p is not None]
    if not parsed:
        raise LibreViewParseError("Ninguna fila tiene un timestamp reconocible.")

    if any(p.g1 >= 1000 for p in parsed):
        return "YMD", warnings

    a_over_12 = any(p.g1 > 12 for p in parsed)
    b_over_12 = any(p.g2 > 12 for p in parsed)

    if a_over_12 and b_over_12:
        raise LibreViewParseError(
            "El archivo mezcla formatos de fecha (hay valores >12 en ambas posiciones). "
            "Probablemente fue re-guardado en Excel. Vuelve a descargarlo de LibreView "
            "sin abrirlo."
        )
    if a_over_12:
        return "DMY", warnings
    if b_over_12:
        return "MDY", warnings

    # Ambiguo: ninguna fecha pasa del dia 12. Ocurre en exports cortos o de principio de mes.
    scores = {order: _density_score(parsed, order) for order in ("DMY", "MDY")}
    best, worst = ("DMY", "MDY") if scores["DMY"] >= scores["MDY"] else ("MDY", "DMY")
    if scores[best] - scores[worst] > 0.15:
        warnings.append(
            f"Orden de fecha ambiguo (ningun dia >12); deducido {best} por densidad "
            f"temporal ({scores[best]:.2f} vs {scores[worst]:.2f})."
        )
        return best, warnings
    if hint in ("DMY", "MDY", "YMD"):
        warnings.append(f"Orden de fecha ambiguo; usando el indicado explicitamente: {hint}.")
        return hint, warnings
    raise LibreViewParseError(
        "No puedo determinar si las fechas son DD-MM-AAAA o MM-DD-AAAA (ninguna pasa del "
        "dia 12 y la serie es demasiado corta). Vuelve a exportar un rango mas largo o "
        "pasa --date-order DMY|MDY explicitamente."
    )


def _density_score(parsed: Sequence[_DateParts], order: str) -> float:
    """Puntua lo plausible que es una interpretacion, en [0, 1].

    Combina tres senales:

    * fechas imposibles bajo ese orden (p.ej. mes 13) -> penalizacion directa;
    * proporcion de intervalos consecutivos "pequenos" (<= 60 min), que es lo normal
      en CGM;
    * **densidad global**: puntos observados frente a los que cabrian en el lapso total
      a la resolucion tipica. Es el termino decisivo: el orden equivocado estira 3 dias
      a 3 meses y hunde la densidad.
    """
    dts = [_assemble(p, order) for p in parsed]
    valid = sorted(d for d in dts if d is not None)
    if len(valid) < 3:
        return 0.0
    invalid_penalty = (len(dts) - len(valid)) / len(dts)

    diffs_min = [(y - x).total_seconds() / 60.0 for x, y in pairwise(valid)]
    small = [d for d in diffs_min if 0 < d <= 60]
    if not small:
        return 0.0
    frac_small = len(small) / len(diffs_min)

    typical_res = sorted(small)[len(small) // 2]
    span_min = (valid[-1] - valid[0]).total_seconds() / 60.0
    expected = span_min / typical_res + 1 if typical_res > 0 else 1.0
    density = min(1.0, len(valid) / expected) if expected > 0 else 0.0

    return max(0.0, (0.4 * frac_small + 0.6 * density) - invalid_penalty)


# --------------------------------------------------------------------------------------
# Lectura del archivo
# --------------------------------------------------------------------------------------


def _decode(data: bytes) -> tuple[str, list[str]]:
    warnings: list[str] = []
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc not in ("utf-8-sig", "utf-8"):
            warnings.append(f"Archivo decodificado como {enc}; el original de LibreView es UTF-8.")
        return text, warnings
    raise LibreViewParseError("No pude decodificar el archivo con ninguna codificacion conocida.")


def _sniff_delimiter(sample: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    counts = {d: sample.count(d) for d in (",", ";", "\t")}
    delim = max(counts, key=lambda d: counts[d])
    if counts[delim] == 0:
        raise LibreViewParseError("El archivo no parece un CSV (no encuentro separadores).")
    if delim != ",":
        warnings.append(
            f"El separador es '{delim}', no ','. Sintoma tipico de haber re-guardado el "
            "archivo en Excel. Lo proceso igual, pero verifica los valores."
        )
    return delim, warnings


def _locate_header(rows: list[list[str]]) -> int:
    """LibreView antepone una o dos lineas de titulo antes de la cabecera real."""
    for i, row in enumerate(rows[:_MAX_PREAMBLE_LINES]):
        joined = _norm(" ".join(row))
        if (
            "timestamp" in joined or "sello de tiempo" in joined or "marca de tiempo" in joined
        ) and len(row) >= 4:
            return i
    raise LibreViewParseError(
        "No encuentro la fila de cabecera en las primeras lineas. El archivo puede estar "
        "truncado o no ser un export de LibreView."
    )


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    # Coma decimal (locale europeo tras re-guardar en Excel) vs coma de millares.
    s = s.replace(",", ".") if ("," in s and "." not in s) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class LibreViewFile:
    readings: list[RawReading]
    food_entries: list[RawFoodEntry]
    rows_parsed: int
    date_order: str
    glucose_unit: str
    warnings: list[str]
    content_sha256: str


def parse_libreview_csv(
    data: bytes,
    *,
    timezone: str,
    date_order_hint: str | None = None,
) -> LibreViewFile:
    """Parsea un export de LibreView a objetos de dominio.

    Args:
        data: contenido crudo del archivo.
        timezone: zona IANA con la que interpretar la hora de pared (p.ej.
            ``America/Mexico_City``). **Obligatoria**: el CSV no la lleva.
        date_order_hint: ``'DMY'`` | ``'MDY'`` | ``'YMD'``, solo se usa si la
            autodeteccion resulta ambigua.
    """
    tz = ZoneInfo(timezone)
    sha = hashlib.sha256(data).hexdigest()
    text, warnings = _decode(data)
    delim, w = _sniff_delimiter(text[:8192])
    warnings += w

    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise LibreViewParseError("El archivo esta vacio.")

    header_idx = _locate_header(rows)
    headers = rows[header_idx]
    body = rows[header_idx + 1 :]
    cols = _build_column_map(headers)

    if cols.historic is None and cols.scan is None:
        raise LibreViewParseError(
            "No encuentro columnas de glucosa (ni historico ni escaneo). Verifica que "
            "descargaste 'Datos de glucosa' y no otro informe."
        )

    raw_ts = [
        r[cols.timestamp] for r in body if len(r) > cols.timestamp and r[cols.timestamp].strip()
    ]
    date_order, w = detect_date_order(raw_ts, hint=date_order_hint)
    warnings += w

    scale = MMOL_TO_MGDL if cols.glucose_unit == "mmol/L" else 1.0
    if scale != 1.0:
        warnings.append("Valores en mmol/L convertidos a mg/dL (x18.0182).")

    readings: list[RawReading] = []
    food: list[RawFoodEntry] = []
    unparsed_ts = 0
    ambiguous_local_times = 0

    for row in body:
        if len(row) <= cols.timestamp:
            continue
        parts = _split_datetime(row[cols.timestamp])
        if parts is None:
            unparsed_ts += 1
            continue
        naive = _assemble(parts, date_order)
        if naive is None:
            unparsed_ts += 1
            continue

        aware = naive.replace(tzinfo=tz)
        # Hora local ambigua (fin del horario de verano: la misma hora de pared ocurre
        # dos veces). Mexico ya no aplica DST, pero otras zonas si: se toma la primera
        # ocurrencia (fold=0) y se contabiliza para avisar al usuario.
        if aware.utcoffset() != naive.replace(tzinfo=tz, fold=1).utcoffset():
            ambiguous_local_times += 1
        ts_utc = aware.astimezone(UTC)
        offset = aware.utcoffset()
        tz_offset = int(offset.total_seconds() // 60) if offset else 0

        serial = _cell(row, cols.serial) or ""
        serial_hash = hashlib.sha256(serial.encode()).hexdigest() if serial else "unknown"

        for col, kind in ((cols.historic, "historic"), (cols.scan, "scan"), (cols.strip, "strip")):
            v = _to_float(_cell(row, col))
            if v is None:
                continue
            readings.append(
                RawReading(
                    ts_utc=ts_utc,
                    tz_offset_min=tz_offset,
                    value_mgdl=v * scale,
                    vendor=VENDOR,
                    device_serial_hash=serial_hash,
                    source_record=kind,
                )
            )

        carbs_g = _to_float(_cell(row, cols.carbs_g))
        carbs_srv = _to_float(_cell(row, cols.carbs_servings))
        note = (_cell(row, cols.notes) or "").strip() or None
        if carbs_g is not None or carbs_srv is not None or note:
            food.append(
                RawFoodEntry(
                    ts_utc=ts_utc,
                    tz_offset_min=tz_offset,
                    carbs_grams=carbs_g,
                    carbs_servings=carbs_srv,
                    note=note,
                )
            )

    if unparsed_ts:
        warnings.append(f"{unparsed_ts} filas descartadas por timestamp ilegible.")
    if ambiguous_local_times:
        warnings.append(
            f"{ambiguous_local_times} horas locales ambiguas (cambio de horario); se tomo "
            "la primera ocurrencia."
        )
    if not readings:
        raise LibreViewParseError("No se extrajo ninguna lectura de glucosa valida.")

    return LibreViewFile(
        readings=sorted(readings, key=lambda r: r.ts_utc),
        food_entries=sorted(food, key=lambda f: f.ts_utc),
        rows_parsed=len(body),
        date_order=date_order,
        glucose_unit=cols.glucose_unit,
        warnings=warnings,
        content_sha256=sha,
    )


def _cell(row: Sequence[str], idx: int | None) -> str | None:
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def iter_historic(readings: Sequence[RawReading]) -> Iterator[RawReading]:
    """Solo el historico continuo.

    Los escaneos y las tiras caen en instantes arbitrarios: incluirlos corrompe la
    deteccion de resolucion nativa y duplica puntos en la ventana postprandial.
    """
    return (r for r in readings if r.source_record == "historic")
