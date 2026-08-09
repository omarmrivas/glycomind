"""Importador de macronutrientes desde USDA FoodData Central.

Existe para que ningun valor nutricional del sistema sea inventado. El catalogo semilla
aporta identidades y alias; los macros vienen de aqui, con su ``fdcId`` guardado en
``food_source_map`` para poder rastrear cada cifra hasta su origen.

Contrato de la API (verificado en https://fdc.nal.usda.gov/api-guide/):

* Base ``https://api.nal.usda.gov/fdc/v1``
* ``GET /foods/search`` con ``query``, ``pageSize``, ``dataType``
* ``GET /food/{fdcId}``
* Autenticacion por ``?api_key=``; se obtiene gratis en api.data.gov
* Limite de **1000 peticiones por hora y por IP**; excederlo devuelve HTTP 429

Limitacion conocida: FoodData Central cubre mal la cocina mexicana casera. Por eso los
platillos compuestos se modelan como recetas y se resuelven por sus ingredientes, que si
estan cubiertos. Las tablas mexicanas (INNSZ, SMAE, IMSS, Tabla extendida 2019) son la
siguiente fuente a integrar.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from glycomind.db.models import Food, FoodNutrient, FoodSourceMap

BASE_URL = "https://api.nal.usda.gov/fdc/v1"
SOURCE = "fdc"

# Identificadores de nutriente en FDC. Se aceptan tanto el id moderno como el
# 'nutrientNumber' heredado, porque conviven segun el endpoint y el conjunto de datos.
NUTRIENT_IDS: dict[str, tuple[set[int], set[str]]] = {
    "energy_kcal": ({1008}, {"208"}),
    "protein": ({1003}, {"203"}),
    "fat": ({1004}, {"204"}),
    "carbohydrate": ({1005}, {"205"}),
    "fiber": ({1079}, {"291"}),
    "sugars": ({2000, 1063}, {"269"}),
}

# Preferencia de conjuntos de datos: los de referencia antes que los de marca.
DEFAULT_DATA_TYPES = ("Foundation", "SR Legacy", "Survey (FNDDS)")


class FdcError(RuntimeError):
    pass


@dataclass(slots=True)
class FdcClient:
    api_key: str
    timeout: float = 20.0
    min_interval_s: float = 0.4
    """El limite es 1000/h; 0.4 s entre llamadas deja margen de sobra."""
    _last_call: float = field(default=0.0, init=False)

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

        query = urllib.parse.urlencode({**params, "api_key": self.api_key}, doseq=True)
        url = f"{BASE_URL}{path}?{query}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise FdcError(
                    "FoodData Central devolvio 429: superaste el limite de 1000 "
                    "peticiones por hora. Reintenta mas tarde."
                ) from exc
            if exc.code in (401, 403):
                raise FdcError(
                    "FoodData Central rechazo la API key. Consigue una gratis en "
                    "https://api.data.gov/signup/"
                ) from exc
            raise FdcError(f"FoodData Central devolvio HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise FdcError(f"No pude contactar FoodData Central: {exc.reason}") from exc
        finally:
            self._last_call = time.monotonic()
        return payload

    def search(
        self, query: str, *, page_size: int = 5, data_types: tuple[str, ...] = DEFAULT_DATA_TYPES
    ) -> list[dict]:
        payload = self._get(
            "/foods/search",
            {"query": query, "pageSize": page_size, "dataType": list(data_types)},
        )
        return payload.get("foods", []) if isinstance(payload, dict) else []

    def food(self, fdc_id: int) -> dict:
        payload = self._get(f"/food/{fdc_id}", {})
        if not isinstance(payload, dict):
            raise FdcError(f"Respuesta inesperada para fdcId={fdc_id}")
        return payload


def extract_nutrients(payload: dict) -> dict[str, tuple[float, str]]:
    """Extrae macros por 100 g. Devuelve ``{nutriente: (cantidad, unidad)}``.

    FDC usa dos formas distintas segun el endpoint y hay que soportar ambas:

    * abreviada  ``{"nutrientId": 1005, "unitName": "G", "value": 23.4}``
    * completa   ``{"nutrient": {"id": 1005, "unitName": "g"}, "amount": 23.4}``
    """
    out: dict[str, tuple[float, str]] = {}
    for entry in payload.get("foodNutrients", []) or []:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("nutrient")
        if isinstance(nested, dict):
            raw_id, raw_number = nested.get("id"), nested.get("number")
            unit, amount = nested.get("unitName"), entry.get("amount")
        else:
            raw_id, raw_number = entry.get("nutrientId"), entry.get("nutrientNumber")
            unit, amount = entry.get("unitName"), entry.get("value")

        if amount is None:
            continue
        number = str(raw_number) if raw_number is not None else None
        for name, (ids, numbers) in NUTRIENT_IDS.items():
            if name in out:
                continue
            if (raw_id in ids) or (number is not None and number in numbers):
                out[name] = (float(amount), str(unit or "").lower() or "g")
                break
    return out


@dataclass(frozen=True, slots=True)
class ImportReport:
    attempted: int
    imported: int
    skipped_existing: int
    not_found: list[str]
    errors: list[str]


def import_nutrients(
    db: Session,
    client: FdcClient,
    *,
    slugs: list[str] | None = None,
    overwrite: bool = False,
    limit: int | None = None,
) -> ImportReport:
    """Rellena ``food_nutrient`` para los alimentos que tengan pista de busqueda.

    Las recetas se omiten: sus macros se derivan de los componentes, que es justo lo que
    permite atribuir la respuesta glucemica a un ingrediente y no al platillo entero.
    """
    stmt = select(Food).where(Food.fdc_query_hint.is_not(None), Food.is_recipe.is_(False))
    if slugs:
        stmt = stmt.where(Food.slug.in_(slugs))
    foods = list(db.execute(stmt.order_by(Food.slug)).scalars())

    already = {
        food_id
        for (food_id,) in db.execute(
            select(FoodNutrient.food_id).where(FoodNutrient.source == SOURCE).distinct()
        )
    }

    attempted = imported = skipped = 0
    not_found: list[str] = []
    errors: list[str] = []

    for food in foods:
        if limit is not None and attempted >= limit:
            break
        if food.id in already and not overwrite:
            skipped += 1
            continue
        attempted += 1
        try:
            hits = client.search(food.fdc_query_hint or food.canonical_name)
            if not hits:
                not_found.append(food.slug)
                continue
            best = hits[0]
            fdc_id = int(best["fdcId"])
            nutrients = extract_nutrients(best) or extract_nutrients(client.food(fdc_id))
            if not nutrients:
                not_found.append(food.slug)
                continue
            _store(db, food, fdc_id, best.get("description"), nutrients, overwrite=overwrite)
            imported += 1
        except FdcError as exc:
            errors.append(f"{food.slug}: {exc}")
            break  # 429 o credenciales: seguir intentando no ayuda
        except (KeyError, ValueError, TypeError) as exc:
            errors.append(f"{food.slug}: respuesta inesperada ({exc})")

    return ImportReport(
        attempted=attempted,
        imported=imported,
        skipped_existing=skipped,
        not_found=not_found,
        errors=errors,
    )


def _store(
    db: Session,
    food: Food,
    fdc_id: int,
    description: str | None,
    nutrients: dict[str, tuple[float, str]],
    *,
    overwrite: bool,
) -> None:
    existing = {
        n.nutrient: n
        for n in db.execute(select(FoodNutrient).where(FoodNutrient.food_id == food.id)).scalars()
    }
    for name, (amount, unit) in nutrients.items():
        row = existing.get(name)
        if row is not None and not overwrite:
            continue
        if row is None:
            row = FoodNutrient(id=uuid.uuid4(), food_id=food.id, nutrient=name)
            db.add(row)
        row.amount_per_100g = amount
        row.unit = unit
        row.source = SOURCE
        row.source_ref = f"fdcId:{fdc_id}"

    mapped = db.execute(
        select(FoodSourceMap).where(
            FoodSourceMap.source == SOURCE, FoodSourceMap.external_id == str(fdc_id)
        )
    ).scalar_one_or_none()
    if mapped is None:
        db.add(
            FoodSourceMap(
                id=uuid.uuid4(),
                food_id=food.id,
                source=SOURCE,
                external_id=str(fdc_id),
                external_name=description,
                matched_by="query_hint",
            )
        )
    db.flush()
