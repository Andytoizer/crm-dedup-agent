from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base
from config.settings import DB_PATH


def get_engine(db_path: str = None):
    path = db_path or DB_PATH
    return create_engine(f"sqlite:///{path}", echo=False)


def init_db(db_path: str = None):
    """Create all tables if they don't exist."""
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


_engine = None
_Session = None


def _get_session_factory():
    global _engine, _Session
    if _Session is None:
        _engine = init_db()
        _Session = sessionmaker(bind=_engine)
    return _Session


@contextmanager
def get_session():
    Session = _get_session_factory()
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
