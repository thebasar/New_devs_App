import asyncio
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import logging
from ..config import settings

logger = logging.getLogger(__name__)


def _async_database_url() -> str:
    """
    Returns the configured DATABASE_URL with an async driver.

    The pool previously assembled its URL from settings.supabase_db_* fields that
    do not exist on Settings, so initialize() raised AttributeError on every call,
    swallowed it, and left session_factory as None. Every revenue query then took
    the 'Database pool not available' path - which used to return hard-coded
    figures - so the dashboard never once read the real database.
    """
    url = settings.database_url
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


class DatabasePool:
    def __init__(self):
        self.engine = None
        self.session_factory = None

    async def initialize(self):
        """Initialize database connection pool (idempotent)."""
        if self.session_factory is not None:
            return

        # create_async_engine supplies its own async-aware pool
        # (AsyncAdaptedQueuePool). Passing the synchronous QueuePool here is
        # rejected by SQLAlchemy, so the pool arguments are left to the default.
        self.engine = create_async_engine(
            _async_database_url(),
            pool_size=20,       # Number of connections to maintain
            max_overflow=30,    # Additional connections when needed
            pool_pre_ping=True, # Validate connections
            pool_recycle=3600,  # Recycle connections every hour
            echo=False          # Set to True for SQL debugging
        )

        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )

        logger.info("Database connection pool initialized")

    async def close(self):
        """Close database connections"""
        if self.engine:
            await self.engine.dispose()
            self.engine = None
            self.session_factory = None

    @asynccontextmanager
    async def get_session(self):
        """Yields a session from the pool and always closes it."""
        if not self.session_factory:
            raise RuntimeError("Database pool not initialized")
        session = self.session_factory()
        try:
            yield session
        finally:
            await session.close()

# Global database pool instance
db_pool = DatabasePool()

async def get_db_session() -> AsyncSession:
    """Dependency to get database session"""
    async with db_pool.get_session() as session:
        yield session
