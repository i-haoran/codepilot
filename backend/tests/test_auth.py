import pytest


@pytest.mark.asyncio
async def test_register(client):
    res = await client.post("/api/v1/auth/register", json={
        "username": "newuser",
        "password": "password123"
    })
    assert res.status_code == 200
    data = res.json()
    assert "token" in data
    assert data["username"] == "newuser"


@pytest.mark.asyncio
async def test_register_duplicate(client):
    await client.post("/api/v1/auth/register", json={
        "username": "dupuser",
        "password": "password123"
    })
    res = await client.post("/api/v1/auth/register", json={
        "username": "dupuser",
        "password": "password456"
    })
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_login(client):
    await client.post("/api/v1/auth/register", json={
        "username": "loginuser",
        "password": "password123"
    })
    res = await client.post("/api/v1/auth/login", json={
        "username": "loginuser",
        "password": "password123"
    })
    assert res.status_code == 200
    assert "token" in res.json()


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    await client.post("/api/v1/auth/register", json={
        "username": "wrongpw",
        "password": "correct"
    })
    res = await client.post("/api/v1/auth/login", json={
        "username": "wrongpw",
        "password": "incorrect"
    })
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_me_unauthorized(client):
    res = await client.get("/api/v1/auth/me")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_me_authorized(auth_client):
    res = await auth_client.get("/api/v1/auth/me")
    assert res.status_code == 200
    assert res.json()["username"] == "testuser"
