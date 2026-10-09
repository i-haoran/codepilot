"""API 路由测试：鉴权保护、会话 CRUD、知识库统计与安全边界。"""


async def test_chat_requires_auth(client):
    """未带 token 访问聊天接口应返回 401。"""
    res = await client.post("/api/v1/chat", json={"message": "你好"})
    assert res.status_code == 401


async def test_conversations_requires_auth(client):
    res = await client.get("/api/v1/conversations")
    assert res.status_code == 401


async def test_health_reports_dependencies(client):
    """健康检查应暴露向量库 / embedding / LLM 配置状态，便于部署排查。"""
    res = await client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    for field in ("version", "vector_store", "embedding", "llm_configured"):
        assert field in data


async def test_create_conversation(auth_client):
    res = await auth_client.post("/api/v1/conversations")
    assert res.status_code == 200
    data = res.json()
    assert data["title"] == "新会话"
    assert "id" in data


async def test_list_conversations(auth_client):
    await auth_client.post("/api/v1/conversations")
    res = await auth_client.get("/api/v1/conversations")
    assert res.status_code == 200
    assert isinstance(res.json(), list)
    assert len(res.json()) >= 1


async def test_delete_conversation(auth_client):
    create_res = await auth_client.post("/api/v1/conversations")
    conv_id = create_res.json()["id"]

    del_res = await auth_client.delete(f"/api/v1/conversations/{conv_id}")
    assert del_res.status_code == 200

    list_res = await auth_client.get("/api/v1/conversations")
    assert conv_id not in [c["id"] for c in list_res.json()]


async def test_delete_missing_conversation_404(auth_client):
    res = await auth_client.delete("/api/v1/conversations/not-exist")
    assert res.status_code == 404


async def test_get_profile(auth_client):
    res = await auth_client.get("/api/v1/profile")
    assert res.status_code == 200
    assert res.json()["username"] == "testuser"


async def test_list_documents_empty(auth_client):
    res = await auth_client.get("/api/v1/documents")
    assert res.status_code == 200
    assert res.json() == []


async def test_knowledge_base_stats_empty(auth_client):
    """新用户的知识库统计应为 0，且返回当前 embedding 模型标识。"""
    res = await auth_client.get("/api/v1/documents/stats")
    assert res.status_code == 200
    data = res.json()
    assert data["document_count"] == 0
    assert data["chunk_count_in_db"] == 0
    assert "embedding" in data


async def test_upload_rejects_empty_file(auth_client):
    """空文件应被拒绝，避免产生 0 分块的脏数据。"""
    res = await auth_client.post(
        "/api/v1/documents",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert res.status_code == 400


async def test_document_upload_and_list(auth_client):
    content = "CodePilot 是一个基于 LangGraph 的研发知识助手。" * 5
    res = await auth_client.post(
        "/api/v1/documents",
        files={"file": ("design.md", content.encode("utf-8"), "text/markdown")},
    )
    assert res.status_code == 200
    doc_id = res.json()["id"]
    assert res.json()["filename"] == "design.md"

    listed = await auth_client.get("/api/v1/documents")
    assert any(d["id"] == doc_id for d in listed.json())

    stats = await auth_client.get("/api/v1/documents/stats")
    assert stats.json()["document_count"] == 1


async def test_path_traversal_avatar_blocked(auth_client):
    """头像接口不应允许越权读取目录外文件。"""
    res = await auth_client.get("/api/v1/avatar/..%2F..%2Frequirements.txt")
    assert res.status_code == 404
