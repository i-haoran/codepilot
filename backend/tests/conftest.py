import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.core.app import app
from app.core.database import Base, get_db
from app.core.auth import hash_password

TEST_DATABASE_URL = "sqlite+aiosqlite:///./test.db"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSession = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with TestSession() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_client(client: AsyncClient, db_session: AsyncSession):
    """返回一个已带 Authorization 头的 client。

    注意：字段名是 password_hash（原代码误写为 hashed_password，
    导致该 fixture 一旦被使用就抛 TypeError）。
    """
    from app.models.user import User
    import uuid

    user = User(
        id=str(uuid.uuid4()),
        username="testuser",
        password_hash=hash_password("testpass123"),
    )
    db_session.add(user)
    await db_session.commit()

    login_res = await client.post("/api/v1/auth/login", json={"username": "testuser", "password": "testpass123"})
    token = login_res.json()["token"]

    client.headers["Authorization"] = f"Bearer {token}"
    return client
