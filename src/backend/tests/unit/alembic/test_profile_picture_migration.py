import importlib

import sqlalchemy as sa


def test_upgrade_replaces_only_legacy_profile_pictures(monkeypatch):
    migration = importlib.import_module("langflow.alembic.versions.f2a3b4c5d6e7_migrate_bird_profile_pictures")
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    user = sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_image", sa.String()),
    )
    metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(
            user.insert(),
            [
                {"profile_image": "People/old.svg"},
                {"profile_image": "Space/old.svg"},
                {"profile_image": "Custom/keep.svg"},
                {"profile_image": None},
            ],
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        migration.upgrade()

        assert conn.execute(sa.select(user.c.profile_image).order_by(user.c.id)).scalars().all() == [
            "Birds/01-owl.svg",
            "Birds/01-owl.svg",
            "Custom/keep.svg",
            None,
        ]
