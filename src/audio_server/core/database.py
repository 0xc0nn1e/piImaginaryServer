from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


@dataclass(frozen=True)
class Database:
    engine: Engine
    session_factory: sessionmaker[Session]

    def session(self) -> Iterator[Session]:
        with self.session_factory() as db_session:
            yield db_session


def create_database(database_url: str, *, echo: bool = False) -> Database:
    connect_args: dict[str, object] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=connect_args,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    return Database(engine=engine, session_factory=factory)
