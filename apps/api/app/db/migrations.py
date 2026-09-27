from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class AppliedMigration:
    version: str


def migration_files(migrations_path: str | Path) -> list[Path]:
    return sorted(Path(migrations_path).glob("*.sql"))


async def run_migrations(engine: AsyncEngine, migrations_path: str | Path) -> list[str]:
    """Apply the repository's plain SQL migrations in lexical version order."""
    files = migration_files(migrations_path)
    if not files:
        return []
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(255) PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        applied = {
            row[0]
            for row in (
                await connection.execute(text("SELECT version FROM schema_migrations"))
            ).all()
        }
        newly_applied: list[str] = []
        for migration in files:
            if migration.name in applied:
                continue
            await connection.exec_driver_sql(migration.read_text(encoding="utf-8"))
            await connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": migration.name},
            )
            newly_applied.append(migration.name)
    return newly_applied


async def migrate_configured_database() -> list[str]:
    from app.db.session import create_engine, dispose_engine
    from app.settings import get_settings

    settings = get_settings()
    engine = create_engine(settings)
    if engine is None:
        raise RuntimeError("DATABASE_URL is required to run migrations.")
    migrations_path = Path(settings.migrations_path)
    if not migrations_path.is_absolute() and not migrations_path.exists():
        migrations_path = Path(__file__).resolve().parents[4] / migrations_path
    try:
        return await run_migrations(engine, migrations_path)
    finally:
        await dispose_engine(engine)


if __name__ == "__main__":
    import asyncio

    applied = asyncio.run(migrate_configured_database())
    print(f"Applied {len(applied)} migration(s): {', '.join(applied) or 'none'}")
