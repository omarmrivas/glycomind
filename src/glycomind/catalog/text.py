"""Normalizacion y troceado del texto libre de una comida.

Puro, sin base de datos, para que sea testeable y predecible. La regla que gobierna
todo el modulo: **el texto crudo del usuario nunca se pierde ni se reescribe**; lo que
se produce aqui son interpretaciones anotadas con su confianza.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Separadores de items dentro de una descripcion. Ojo: ' con ' NO esta aqui porque
# forma parte de nombres de platillos ("arroz con pollo", "frijoles con chorizo"). El
# resolutor intenta primero la cadena completa y solo despues trocea.
_SPLIT_RE = re.compile(r"\s*(?:,|;|\+|\||\by\b|\be\b|/)\s*", re.IGNORECASE)

# Separador de ULTIMO recurso. Solo se usa cuando el segmento entero no resuelve y el
# troceo produce mas coincidencias exactas: asi "arroz con pollo" sobrevive entero pero
# "avena con platano" se parte en dos alimentos reales.
_CONNECTOR_RE = re.compile(r"\s+(?:con|sin|acompanado de|acompanada de|mas)\s+", re.IGNORECASE)

_NUMBER_WORDS = {
    "un": 1.0,
    "uno": 1.0,
    "una": 1.0,
    "dos": 2.0,
    "tres": 3.0,
    "cuatro": 4.0,
    "cinco": 5.0,
    "seis": 6.0,
    "medio": 0.5,
    "media": 0.5,
    "cuarto": 0.25,
}

# Unidad canonica -> variantes tal como las escribe la gente.
_UNITS = {
    "g": ("g", "gr", "grs", "gramo", "gramos"),
    "ml": ("ml", "mililitro", "mililitros"),
    "taza": ("taza", "tazas"),
    "cucharada": ("cucharada", "cucharadas", "cda", "cdas"),
    "cucharadita": ("cucharadita", "cucharaditas", "cdta", "cdtas"),
    "pieza": ("pieza", "piezas", "pza", "pzas"),
    "rebanada": ("rebanada", "rebanadas"),
    "porcion": ("porcion", "porciones"),
    "plato": ("plato", "platos"),
    "vaso": ("vaso", "vasos"),
}
_UNIT_LOOKUP = {v: canonical for canonical, variants in _UNITS.items() for v in variants}

# Las alternativas van de mas larga a mas corta: la alternancia de regex es ordenada y
# con 'un' antes que 'una', "una manzana" se parseaba como 1 + unidad "a" + "manzana".
_NUMBER_ALTERNATION = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))

_QUANTITY_RE = re.compile(
    r"^\s*(?P<num>\d+(?:[.,]\d+)?|(?:" + _NUMBER_ALTERNATION + r")\b)"
    r"(?:\s*(?P<unit>[a-zA-Zñáéíóú]+))?\s+(?P<rest>.+)$",
    re.IGNORECASE,
)

# Ruido que no aporta identidad al alimento.
_STOPWORDS = frozenset(
    {"de", "del", "la", "el", "los", "las", "al", "a", "en", "un", "una", "mi", "mis"}
)


def normalize(text: str) -> str:
    """Minusculas, sin acentos, sin puntuacion, espacios colapsados.

    Es la forma con la que se compara contra ``food_alias.normalized``.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_stopwords(normalized: str) -> str:
    """Quita articulos y preposiciones. 'tortilla de maiz' -> 'tortilla maiz'."""
    kept = [w for w in normalized.split() if w not in _STOPWORDS]
    return " ".join(kept) if kept else normalized


@dataclass(frozen=True, slots=True)
class ParsedItem:
    """Un item tal como se extrajo del texto, antes de resolverlo contra el catalogo."""

    raw: str
    label: str
    """Texto del item sin la cantidad. Es lo que se busca en el catalogo."""
    normalized: str
    quantity_value: float | None
    quantity_unit: str | None
    preparation: str | None


_PREPARATIONS = {
    "frito": ("frito", "fritos", "frita", "fritas", "fritura"),
    "asado": ("asado", "asada", "asados", "asadas", "a la plancha", "plancha", "parrilla"),
    "hervido": ("hervido", "hervida", "cocido", "cocida", "de la olla"),
    "horneado": ("horneado", "horneada", "al horno"),
    "crudo": ("crudo", "cruda", "fresco", "fresca"),
    "capeado": ("capeado", "capeada", "empanizado", "empanizada"),
}
_PREP_LOOKUP = {v: canonical for canonical, variants in _PREPARATIONS.items() for v in variants}


def detect_preparation(normalized: str) -> str | None:
    """La preparacion cambia la respuesta glucemica (freir anade grasa, que la retrasa)."""
    for variant, canonical in _PREP_LOOKUP.items():
        if re.search(rf"\b{re.escape(variant)}\b", normalized):
            return canonical
    return None


def parse_quantity(text: str) -> tuple[float | None, str | None, str]:
    """Extrae la cantidad inicial. Devuelve ``(valor, unidad, resto)``.

    Contar objetos discretos ("2 tortillas") es mucho mas fiable que estimar volumen,
    asi que reconocer la cantidad explicita del usuario es la mejor senal disponible.
    """
    m = _QUANTITY_RE.match(text)
    if not m:
        return None, None, text.strip()

    raw_num = m.group("num").lower().replace(",", ".")
    value = _NUMBER_WORDS.get(raw_num)
    if value is None:
        try:
            value = float(raw_num)
        except ValueError:
            return None, None, text.strip()

    raw_unit = (m.group("unit") or "").lower()
    rest = m.group("rest").strip()
    unit = _UNIT_LOOKUP.get(normalize(raw_unit)) if raw_unit else None
    if raw_unit and unit is None:
        # La palabra no era unidad sino parte del alimento ("2 huevos estrellados").
        rest = f"{raw_unit} {rest}".strip()
    return value, unit, rest


def split_items(text: str) -> list[str]:
    """Trocea una descripcion en items. Conserva el orden de aparicion."""
    parts = [p.strip() for p in _SPLIT_RE.split(text) if p and p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def split_on_connector(text: str) -> list[str]:
    """Trocea por ' con ' / ' sin '. Solo para el respaldo del resolutor."""
    parts = [p.strip() for p in _CONNECTOR_RE.split(text) if p and p.strip()]
    return parts if len(parts) > 1 else []


def _strip_leading_stopwords(text: str) -> str:
    """Quita conectores sobrantes al inicio: 'de frijoles negros' -> 'frijoles negros'.

    Solo al inicio: 'frijoles de la olla' no se toca, que es un nombre real.
    """
    words = text.split()
    while words and normalize(words[0]) in _STOPWORDS:
        words.pop(0)
    return " ".join(words) if words else text


def parse_item(raw: str) -> ParsedItem:
    quantity, unit, rest = parse_quantity(raw)
    rest = _strip_leading_stopwords(rest)
    norm = normalize(rest)
    return ParsedItem(
        raw=raw.strip(),
        label=rest,
        normalized=norm,
        quantity_value=quantity,
        quantity_unit=unit,
        preparation=detect_preparation(norm),
    )


def parse_meal_text(text: str) -> list[ParsedItem]:
    """Trocea y parsea una descripcion completa de comida."""
    return [parse_item(p) for p in split_items(text)]
