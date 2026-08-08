"""CLI de GlycoMind (Fase 1)."""

from __future__ import annotations

import contextlib
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from glycomind.analysis.pipeline import analyze_user
from glycomind.config import ALGORITHM_VERSION, settings
from glycomind.db.models import AppUser, Meal, MealGlucoseResponse
from glycomind.db.session import db_session
from glycomind.domain.enums import MealType, ResponseQuality
from glycomind.ingest.libreview import LibreViewParseError
from glycomind.ingest.service import import_libreview_csv


def _force_utf8_output() -> None:
    """La consola de Windows usa cp1252 por defecto y no puede imprimir 'Δ', '≥' ni '→'.

    Sin esto, el CLI revienta al mostrar resultados correctos, que es la peor clase de
    fallo: el trabajo esta hecho pero parece roto.
    """
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None)
        if enc and enc.lower().replace("-", "") != "utf8":
            with contextlib.suppress(AttributeError, OSError):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


_force_utf8_output()

app = typer.Typer(
    help="Pipeline determinista comida <-> respuesta glucemica.", no_args_is_help=True
)
db_app = typer.Typer(help="Gestion del esquema.", no_args_is_help=True)
user_app = typer.Typer(help="Usuarios.", no_args_is_help=True)
meal_app = typer.Typer(help="Comidas.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(user_app, name="user")
app.add_typer(meal_app, name="meal")

console = Console()


def _get_user(db, email: str) -> AppUser:
    user = db.execute(select(AppUser).where(AppUser.email == email)).scalar_one_or_none()
    if user is None:
        raise typer.BadParameter(
            f"No existe el usuario {email}. Crealo con 'glycomind user create'."
        )
    return user


@db_app.command("create-all")
def db_create_all() -> None:
    """Crea el esquema directamente (solo desarrollo; en produccion usa Alembic)."""
    from glycomind.db.models import Base
    from glycomind.db.session import engine

    Base.metadata.create_all(engine)
    console.print("[green]Esquema creado.[/green]")


@db_app.command("apply-views")
def db_apply_views(
    path: Annotated[Path, typer.Option(help="Archivo SQL de vistas.")] = Path("sql/views.sql"),
) -> None:
    """(Re)crea las vistas de lectura usadas por Grafana. Idempotente."""
    from sqlalchemy import text

    from glycomind.db.session import engine

    sql = path.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(text(sql))
    console.print(f"[green]Vistas aplicadas desde {path}.[/green]")


@user_app.command("create")
def user_create(
    email: Annotated[str, typer.Option(help="Identificador del usuario.")],
    timezone: Annotated[str, typer.Option(help="Zona IANA.")] = settings.default_timezone,
    name: Annotated[str | None, typer.Option(help="Nombre para mostrar.")] = None,
) -> None:
    """Crea un usuario.

    Minimizacion de datos: solo correo, alias y zona horaria. Ni nombre completo, ni
    direccion, ni CURP. Ver docs/08-regulatorio.md.
    """
    ZoneInfo(timezone)
    with db_session() as db:
        if db.execute(select(AppUser).where(AppUser.email == email)).scalar_one_or_none():
            console.print(f"[yellow]{email} ya existe.[/yellow]")
            return
        db.add(AppUser(id=uuid.uuid4(), email=email, display_name=name, timezone=timezone))
    console.print(f"[green]Usuario {email} creado ({timezone}).[/green]")


@app.command("import-libreview")
def import_libreview(
    path: Annotated[Path, typer.Argument(help="CSV descargado de LibreView.")],
    email: Annotated[str, typer.Option("--user", help="Correo del usuario.")],
    timezone: Annotated[str | None, typer.Option(help="Sobrescribe la zona del usuario.")] = None,
    date_order: Annotated[
        str | None,
        typer.Option(help="DMY|MDY|YMD. Solo si la autodeteccion resulta ambigua."),
    ] = None,
) -> None:
    """Importa un export de LibreView.

    La exportacion de Abbott es manual y esta protegida por reCAPTCHA, asi que no puede
    automatizarse. Reimportar archivos solapados es seguro e idempotente.
    """
    data = path.read_bytes()
    with db_session() as db:
        user = _get_user(db, email)
        try:
            result = import_libreview_csv(
                db,
                user=user,
                data=data,
                filename=path.name,
                timezone=timezone,
                date_order_hint=date_order,
            )
        except LibreViewParseError as exc:
            console.print(f"[red]No pude leer el archivo:[/red] {exc}")
            raise typer.Exit(code=1) from exc

    table = Table(title=f"Importacion — {path.name}", show_header=False)
    table.add_row("Filas leidas", str(result.rows_parsed))
    table.add_row("Lecturas encontradas", str(result.readings_found))
    table.add_row("Lecturas nuevas", f"[green]{result.readings_inserted}[/green]")
    table.add_row("Ya conocidas", str(result.readings_duplicate))
    table.add_row("Sensores nuevos", str(result.sessions_created))
    table.add_row("Resolucion detectada", f"{result.detected_resolution_min} min")
    table.add_row("Comidas desde la app", str(result.food_entries_inserted))
    if result.first_ts_utc and result.last_ts_utc:
        table.add_row(
            "Rango (UTC)",
            f"{result.first_ts_utc:%Y-%m-%d %H:%M} → {result.last_ts_utc:%Y-%m-%d %H:%M}",
        )
    console.print(table)
    for w in result.warnings:
        console.print(f"[yellow]aviso:[/yellow] {w}")


@meal_app.command("add")
def meal_add(
    email: Annotated[str, typer.Option("--user")],
    at: Annotated[str, typer.Option(help="Hora local: 'YYYY-MM-DD HH:MM'.")],
    text: Annotated[str, typer.Option(help="Descripcion libre.")],
    meal_type: Annotated[MealType, typer.Option(help="Tipo de comida.")] = MealType.UNKNOWN,
) -> None:
    """Registra una comida en hora local del usuario."""
    with db_session() as db:
        user = _get_user(db, email)
        tz = ZoneInfo(user.timezone)
        local = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        offset = local.utcoffset()
        db.add(
            Meal(
                id=uuid.uuid4(),
                user_id=user.id,
                consumed_at=local,
                tz_offset_min=int(offset.total_seconds() // 60) if offset else 0,
                meal_type=meal_type.value,
                free_text=text,
                source="cli",
                entry_completeness=0.5,
            )
        )
    console.print(f"[green]Comida registrada:[/green] {at} — {text}")


@meal_app.command("import")
def meal_import(
    path: Annotated[Path, typer.Argument(help="Archivo 'YYYY-MM-DD HH:MM|descripcion' por linea.")],
    email: Annotated[str, typer.Option("--user")],
) -> None:
    """Carga comidas en lote desde un archivo de texto.

    Pensado para rellenar historico. La captura del dia a dia deberia tener mucha menos
    friccion (foto + un toque): si registrar cuesta, el usuario deja de hacerlo y el
    ratio de ventanas validas se hunde.
    """
    added = skipped = 0
    with db_session() as db:
        user = _get_user(db, email)
        tz = ZoneInfo(user.timezone)
        existing = {
            ts for (ts,) in db.execute(select(Meal.consumed_at).where(Meal.user_id == user.id))
        }
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            raw_ts, _, text = line.partition("|")
            try:
                local = datetime.strptime(raw_ts.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            except ValueError:
                console.print(f"[yellow]linea {lineno}: fecha ilegible, omitida[/yellow]")
                continue
            if local in existing:
                skipped += 1
                continue
            offset = local.utcoffset()
            db.add(
                Meal(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    consumed_at=local,
                    tz_offset_min=int(offset.total_seconds() // 60) if offset else 0,
                    free_text=text.strip() or None,
                    source="bulk_import",
                    entry_completeness=0.5,
                )
            )
            existing.add(local)
            added += 1
    console.print(f"[green]{added} comidas anadidas[/green], {skipped} ya existian.")


@app.command("analyze")
def analyze(
    email: Annotated[str, typer.Option("--user")],
    recompute: Annotated[bool, typer.Option(help="Recalcula todo, ignorando lo ya hecho.")] = False,
) -> None:
    """Empareja comidas con ventanas glucemicas y calcula metricas."""
    with db_session() as db:
        user = _get_user(db, email)
        summary = analyze_user(db, user_id=user.id, recompute=recompute)

    table = Table(title=f"Analisis — {ALGORITHM_VERSION}", show_header=False)
    table.add_row("Comidas", str(summary.meals_total))
    table.add_row("Calculadas ahora", str(summary.computed))
    table.add_row("Ya calculadas", str(summary.skipped_existing))
    table.add_row("Utilizables", f"[green]{summary.usable}[/green]")
    table.add_row("  de ellas, degradadas", str(summary.degraded))
    table.add_row("Excluidas", f"[red]{summary.excluded}[/red]")
    console.print(table)

    ratio = summary.pairing_valid_ratio
    color = "green" if ratio >= 0.6 else "red"
    console.print(f"\n[bold]pairing_valid_ratio: [{color}]{ratio:.0%}[/{color}][/bold]")
    if summary.computed and ratio < 0.6:
        console.print(
            "[yellow]Por debajo del 60%: el cuello de botella es la captura de comidas o "
            "la adherencia al sensor, no la estadistica.[/yellow]"
        )

    if summary.exclusion_counts:
        ex = Table(title="Por que se excluyeron")
        ex.add_column("Razon")
        ex.add_column("N", justify="right")
        for reason, count in summary.exclusion_counts.items():
            ex.add_row(reason, str(count))
        console.print(ex)


@app.command("report")
def report(
    email: Annotated[str, typer.Option("--user")],
    limit: Annotated[int, typer.Option(help="Cuantas respuestas mostrar.")] = 20,
) -> None:
    """Lista las respuestas postprandiales calculadas."""
    with db_session() as db:
        user = _get_user(db, email)
        rows = db.execute(
            select(MealGlucoseResponse, Meal)
            .join(Meal, Meal.id == MealGlucoseResponse.meal_id)
            .where(MealGlucoseResponse.user_id == user.id)
            .order_by(Meal.consumed_at.desc())
            .limit(limit)
        ).all()

    if not rows:
        console.print("[yellow]No hay respuestas calculadas. Ejecuta 'glycomind analyze'.[/yellow]")
        return

    table = Table(title=f"Respuestas postprandiales — {email}")
    for col in ("Fecha local", "Comida", "Calidad", "Basal", "Δ pico", "TTP", "iAUC-120", "Forma"):
        table.add_column(col)
    for resp, meal in rows:
        local = meal.consumed_at.astimezone(ZoneInfo(user.timezone))
        quality_color = {
            ResponseQuality.OK.value: "green",
            ResponseQuality.DEGRADED.value: "yellow",
            ResponseQuality.EXCLUDED.value: "red",
        }[resp.quality]
        detail = (
            ", ".join(resp.exclusion_reasons)
            if resp.quality == ResponseQuality.EXCLUDED.value
            else resp.quality
        )
        # El pico a resolucion gruesa es una COTA INFERIOR: se marca con '>='.
        peak = "—"
        if resp.peak_delta_mgdl is not None:
            prefix = "≥" if resp.peak_underestimated else ""
            peak = f"{prefix}{resp.peak_delta_mgdl:+.0f}"
        table.add_row(
            f"{local:%Y-%m-%d %H:%M}",
            (meal.free_text or "—")[:32],
            f"[{quality_color}]{detail}[/{quality_color}]",
            f"{resp.baseline_mgdl:.0f}" if resp.baseline_mgdl else "—",
            peak,
            f"{resp.time_to_peak_min:.0f}m" if resp.time_to_peak_min is not None else "—",
            f"{resp.iauc_120:.0f}" if resp.iauc_120 is not None else "—",
            resp.curve_shape or "—",
        )
    console.print(table)
    console.print(
        "\n[dim]≥ indica que el pico es una cota inferior: a 15 min de resolucion el "
        "maximo real cae entre muestras.[/dim]"
    )


if __name__ == "__main__":
    app()
