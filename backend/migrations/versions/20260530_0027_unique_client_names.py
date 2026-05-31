from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260530_0027"
down_revision: str | None = "20260530_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _deduplicate_client_names() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            WITH ranked AS (
                SELECT
                    id,
                    name,
                    row_number() OVER (
                        PARTITION BY name
                        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
                    ) AS duplicate_rank
                FROM clients
            )
            UPDATE clients AS clients_table
            SET name = left(ranked.name, 216) || ' (' || clients_table.id::text || ')'
            FROM ranked
            WHERE clients_table.id = ranked.id
              AND ranked.duplicate_rank > 1
            """
        )
        return

    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                name,
                row_number() OVER (
                    PARTITION BY name
                    ORDER BY updated_at DESC, created_at DESC, id DESC
                ) AS duplicate_rank
            FROM clients
        )
        UPDATE clients
        SET name = substr((SELECT ranked.name FROM ranked WHERE ranked.id = clients.id), 1, 216)
            || ' (' || id || ')'
        WHERE id IN (SELECT id FROM ranked WHERE duplicate_rank > 1)
        """
    )


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    _deduplicate_client_names()
    if dialect == "postgresql":
        op.create_unique_constraint("uq_clients_name", "clients", ["name"])
        return

    if dialect == "sqlite":
        with op.batch_alter_table("clients") as batch_op:
            batch_op.create_unique_constraint("uq_clients_name", ["name"])
        return

    op.create_unique_constraint("uq_clients_name", "clients", ["name"])


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.drop_constraint("uq_clients_name", "clients", type_="unique")
        return

    if dialect == "sqlite":
        with op.batch_alter_table("clients") as batch_op:
            batch_op.drop_constraint("uq_clients_name", type_="unique")
        return

    op.drop_constraint("uq_clients_name", "clients", type_="unique")
