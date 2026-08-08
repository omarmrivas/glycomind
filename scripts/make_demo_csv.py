"""Genera un CSV con la forma de un export de LibreView, para desarrollo.

No sustituye a datos reales: sirve para ejercitar el pipeline de punta a punta antes de
tener 14 dias de sensor puesto. Reproduce las caracteristicas que importan del
FreeStyle Libre 2 Plus: resolucion de 15 min, sensores de 15 dias, huecos de cobertura
y algunas comidas registradas desde la app.

    uv run python scripts/make_demo_csv.py data/demo.csv
"""

from __future__ import annotations

import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from factories import build_libreview_csv

RESOLUTION_MIN = 15
DAYS = 14
START = datetime(2026, 7, 20, 0, 0)

# (hora, minuto, amplitud media mg/dL, minutos hasta el pico, etiqueta)
DAILY_MEALS = [
    (7, 30, 55.0, 60.0, "avena con platano y leche"),
    (13, 20, 48.0, 75.0, "tortillas, frijoles, pollo, aguacate"),
    (20, 15, 62.0, 70.0, "arroz blanco con pollo"),
]


def gamma_bump(dt_min: float, amp: float, ttp: float) -> float:
    if dt_min <= 0:
        return 0.0
    k = 2.0
    theta = ttp / k
    return amp * (dt_min / ttp) ** k * math.exp(k - dt_min / theta)


def main(out_path: str) -> None:
    rng = random.Random(20260808)
    readings: list[tuple[datetime, float]] = []
    foods: list[tuple[datetime, float | None, str | None]] = []
    meal_events: list[tuple[datetime, float, float]] = []

    for day in range(DAYS):
        day_start = START + timedelta(days=day)
        for hour, minute, amp, ttp, label in DAILY_MEALS:
            jitter = rng.randint(-25, 25)
            t = day_start.replace(hour=hour, minute=minute) + timedelta(minutes=jitter)
            # Variabilidad intraindividual deliberada: es EL fenomeno del problema.
            # Con ICC ~0.3 la respuesta a la misma comida varia enormemente.
            actual_amp = max(10.0, rng.gauss(amp, amp * 0.45))
            actual_ttp = max(30.0, rng.gauss(ttp, 15.0))
            meal_events.append((t, actual_amp, actual_ttp))
            # Un tercio de las comidas se registra en la app de Abbott.
            if rng.random() < 0.33:
                foods.append((t, round(rng.uniform(25, 70), 0), label))

    n_steps = int(DAYS * 24 * 60 / RESOLUTION_MIN)
    for i in range(n_steps):
        t = START + timedelta(minutes=i * RESOLUTION_MIN)

        # Hueco de cobertura de ~2 h cada tres dias (telefono lejos, ducha, etc.).
        if (i * RESOLUTION_MIN) % (3 * 24 * 60) < 120 and i > 0:
            continue

        circadian = 4.0 * math.sin(2 * math.pi * (t.hour + t.minute / 60) / 24 - 1.2)
        value = 92.0 + circadian + rng.gauss(0, 2.5)
        for meal_t, amp, ttp in meal_events:
            value += gamma_bump((t - meal_t).total_seconds() / 60.0, amp, ttp)
        readings.append((t, round(max(45.0, min(320.0, value)), 1)))

    # Dos sensores de 15 dias: el cambio de serie delimita las sesiones.
    mid = len(readings) // 2
    csv_a = build_libreview_csv(
        readings=readings[:mid],
        serial="SENSOR0001",
        date_order="DMY",
        foods=[f for f in foods if f[0] < readings[mid][0]],
    )
    csv_b = build_libreview_csv(
        readings=readings[mid:],
        serial="SENSOR0002",
        date_order="DMY",
        foods=[f for f in foods if f[0] >= readings[mid][0]],
    )
    # LibreView entrega un unico archivo con todos los sensores del periodo.
    merged = csv_a + b"\n" + b"\n".join(csv_b.split(b"\n")[2:])

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(merged)
    print(f"{out}: {len(readings)} lecturas, {len(foods)} comidas de la app, 2 sensores")

    plan = out.with_suffix(".meals.txt")
    plan.write_text(
        "\n".join(
            f"{t:%Y-%m-%d %H:%M}|{label}"
            for (t, _, _), label in zip(
                meal_events, [m[4] for _ in range(DAYS) for m in DAILY_MEALS], strict=True
            )
        ),
        encoding="utf-8",
    )
    print(f"{plan}: {len(meal_events)} comidas para registrar")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/demo.csv")
