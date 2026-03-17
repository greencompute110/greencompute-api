from __future__ import annotations

from sqlalchemy import select

from greenference_persistence import create_db_engine, create_session_factory, init_database, session_scope
from greenference_persistence.orm import BuildORM
from greenference_protocol import BuildRecord


class BuilderRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.engine = create_db_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        init_database(self.engine)

    def save_build(self, build: BuildRecord) -> BuildRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(BuildORM, build.build_id) or BuildORM(build_id=build.build_id)
            row.image = build.image
            row.context_uri = build.context_uri
            row.dockerfile_path = build.dockerfile_path
            row.public = build.public
            row.status = build.status
            row.artifact_uri = build.artifact_uri
            row.created_at = build.created_at
            row.updated_at = build.updated_at
            session.add(row)
        return build

    def list_builds(self) -> list[BuildRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(BuildORM)).all()
            return [
                BuildRecord(
                    build_id=row.build_id,
                    image=row.image,
                    context_uri=row.context_uri,
                    dockerfile_path=row.dockerfile_path,
                    public=row.public,
                    status=row.status,
                    artifact_uri=row.artifact_uri,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ]
