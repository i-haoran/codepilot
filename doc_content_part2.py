# -*- coding: utf-8 -*-
"""
《CodePilot 项目详解》内容定义 —— 第二部分：
数据层、缓存与降级、安全设计、工程实践、面试题汇总、附录。

最终 BLOCKS = 第一部分 + 第二部分（由 doc_content.py 汇总导出）。
"""

BLOCKS_PART2 = [

# ==================================================================== #
("h1", "数据层设计", None),
# ==================================================================== #

("h2", "6.1 数据库连接与异步 ORM"),
("p", "使用 SQLAlchemy 2.0 的异步引擎，驱动为 **asyncpg**。"
      "聊天接口是典型的 IO 密集型：等待 LLM 生成、等待数据库查询、等待向量库检索，"
      "异步能在等待期间释放事件循环，显著提升并发吞吐。"),
("code", """
# backend/app/core/database.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    \"\"\"FastAPI 依赖：每个请求一个会话，请求结束自动关闭。\"\"\"
    async with async_session() as session:
        yield session


async def init_db():
    \"\"\"启动时建表（生产环境应改用 Alembic 迁移）。\"\"\"
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
""", "核心代码 6-1　异步数据库引擎与会话依赖"),
("note", "`expire_on_commit=False` 是异步场景下的必要设置："
         "默认 True 会在提交后使对象属性过期，后续访问属性会触发**隐式 IO**，"
         "在异步上下文里会导致 `MissingGreenlet` 报错。", "tip"),

("h2", "6.2 表结构设计"),
("code", """
users                          conversations
┌──────────────────────┐       ┌──────────────────────┐
│ id            PK     │◄──┐   │ id            PK     │
│ username   UNIQUE IDX│   │   │ user_id        IDX   │
│ password_hash        │   │   │ title                │
│ avatar               │   │   │ created_at           │
│ created_at           │   │   │ updated_at           │
└──────────────────────┘   │   └──────────────────────┘
                           │              ▲
                           │              │ conversation_id (IDX)
                           │   ┌──────────┴───────────┐
                           │   │ chat_history         │
                           │   │ id            PK     │
                           ├───│ user_id        IDX   │
                           │   │ conversation_id IDX  │
                           │   │ message        TEXT  │
                           │   │ response       TEXT  │
                           │   │ created_at           │
                           │   └──────────────────────┘
                           │
                           │   ┌──────────────────────┐
                           └───│ documents            │
                               │ id            PK     │
                               │ user_id        IDX   │
                               │ filename             │
                               │ content        TEXT  │
                               │ chunk_count          │
                               │ created_at           │
                               └──────────────────────┘

向量数据（与关系库分离存储，通过 metadata 关联）
Chroma collection: codepilot_knowledge
  metadata: { user_id, doc_id, chunk_index, total_chunks, filename }
""", "图 6-1　数据模型与向量库关联"),

("table",
 ["表名", "字段", "类型 / 约束", "说明"],
 [
  ["users", "id", "String(36) PK", "UUID 主键"],
  ["", "username", "String(64) UNIQUE IDX", "登录名，唯一索引加速查询"],
  ["", "password_hash", "String(128)", "bcrypt 哈希，**绝不存明文**"],
  ["", "avatar", "String(512)", "头像相对路径"],
  ["", "created_at", "DateTime server_default=now()", "由数据库生成时间"],
  ["conversations", "id / user_id / title", "String PK / String(64) IDX / String(256)", "会话，标题取首条消息前 50 字"],
  ["", "created_at / updated_at", "DateTime + onupdate=now()", "updated_at 自动更新，用于会话列表排序"],
  ["chat_history", "id / user_id / conversation_id", "String PK / IDX / IDX", "问答记录，双索引支持按会话与按用户查询"],
  ["", "message / response", "Text", "用户问题与模型回答"],
  ["documents", "id / user_id / filename", "String PK / IDX / String(256)", "文档元数据"],
  ["", "content", "Text", "原始全文，用于全文检索工具"],
  ["", "chunk_count", "Integer default=0", "分块数，删除时用于回退清理"],
 ],
 [2.6, 4.2, 4.8, 5.0]),

("h2", "6.3 建立索引的理由"),
("ul", [
    "`users.username` 唯一索引：登录/注册都要按用户名查询，且唯一性由数据库保证",
    "`conversations.user_id`：会话列表按用户过滤并排序",
    "`chat_history.conversation_id`：拉取某会话的历史消息（最高频查询）",
    "`chat_history.user_id`：按用户维度做统计与隔离校验",
    "`documents.user_id`：知识库列表与统计",
]),
("note", "追问「N+1 查询怎么避免」时：当前数据量小、关系简单（没有复杂的嵌套关联查询），"
         "所以没有显式使用 `selectinload`。正确回答是：**承认现状 + 说出方案**——"
         "「如果后续要一次查出会话及其消息列表，我会用 `selectinload` 预加载，"
         "避免在循环里逐条查询。」", "info"),

("h3", "索引原理（面试常问）"),
("p", "PostgreSQL / MySQL 的普通索引底层是 **B+ 树**。选它的原因："),
("ul", [
    "**树高低**：B+ 树是多路平衡树，一个节点存多个键，3~4 层就能覆盖千万级数据，"
    "而二叉树同样数据量需要 20+ 层——**树高直接决定磁盘 IO 次数**",
    "**叶子节点串联成链表**：天然支持范围查询（`WHERE created_at > '...'`）和排序，"
    "找到起点后顺序扫描链表即可，不需要回到根节点",
    "**数据只在叶子节点**：内部节点只存键和指针，单个节点能容纳更多键，进一步降低树高",
    "**相比 B 树**：B 树的非叶子节点也存数据，同样大小的节点能放的键更少，树更高",
    "**相比哈希索引**：哈希索引等值查询是 O(1)，但**不支持范围查询和排序**，"
    "而实际业务里范围查询非常常见",
]),
("p", "对应的实践结论：`WHERE user_id = ?` 这类等值查询走索引；"
      "而 `LIKE '%keyword%'` 前置通配符**无法使用 B+ 树索引**（无法定位起点），"
      "本项目里全文检索工具的 `ilike('%keyword%')` 就是这种情况——"
      "数据量大时应改用全文索引（PostgreSQL 的 GIN / tsvector）或搜索引擎。"),
("note", "「最左前缀原则」也是高频追问：联合索引 `(a, b, c)` 能用于 `a`、`a,b`、`a,b,c` 的查询，"
         "但单独查 `b` 或 `c` 用不上——因为 B+ 树是按 a 排序、a 相同再按 b 排序的。", "tip"),

("h2", "6.4 文档上传与向量写入的完整流程"),
("code", """
@router.post("/documents", response_model=DocumentResponse)
async def upload_document(file: UploadFile = File(...),
                          user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    raw = await file.read()

    # ① 大小限制：防止超大文件占满内存与向量库
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件过大，请上传 5MB 以内的文本文件")

    # ② 解码并校验非空
    text = raw.decode("utf-8", errors="ignore")
    if not text.strip():
        raise HTTPException(status_code=400, detail="文件内容为空或不是 UTF-8 文本")

    # ③ 分块（递归分块 512/50）
    processor = DocumentProcessor()
    chunks = processor.process(text, metadata={"filename": file.filename, "user_id": user.id})

    # ④ 先落关系库，拿到 doc_id
    doc = Document(user_id=user.id, filename=file.filename,
                   content=text, chunk_count=len(chunks))
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    # ⑤ 再批量写入向量库（用 doc_id 关联）
    retriever = RAGRetriever()
    written = retriever.add_chunks(chunks, doc_id=doc.id, user_id=user.id)
    if written != len(chunks):
        logger.warning("向量写入数量与分块数不一致: written=%s chunks=%s", written, len(chunks))

    return DocumentResponse(id=doc.id, filename=doc.filename,
                            chunk_count=doc.chunk_count,
                            created_at=doc.created_at.isoformat())
""", "核心代码 6-2　文档上传：分块 → 落库 → 向量化"),
("p", "顺序设计说明：**先写关系库拿到 doc_id，再写向量库**。"
      "因为分块的向量 ID 需要以 `doc_id` 为前缀（`{doc_id}_chunk_{i}`），"
      "并且元数据里要写入 `doc_id` 用于后续按文档删除。"),
("h3", "批量写入而非逐条写入"),
("code", """
def add_chunks(self, chunks: List[Document], doc_id: str, user_id: str) -> int:
    \"\"\"写入一个文档的所有分块，返回写入数量。\"\"\"
    if not self.collection or not chunks:
        return 0

    ids, docs, metas = [], [], []
    for index, chunk in enumerate(chunks):
        metadata = dict(chunk.metadata or {})
        metadata.update({"doc_id": doc_id, "user_id": user_id, "chunk_index": index})
        ids.append(f"{doc_id}_chunk_{index}")
        docs.append(chunk.page_content)
        metas.append(metadata)

    # 单次批量写入，避免逐条 add 带来的 N 次索引提交
    self.collection.add(ids=ids, documents=docs, metadatas=metas)
    return len(ids)
""", "核心代码 6-3　向量批量写入"),

("h2", "6.5 删除文档：按元数据过滤而非数数"),
("code", """
def delete_by_doc_id(self, doc_id: str, chunk_count: Optional[int] = None) -> int:
    \"\"\"
    按 doc_id 删除该文档的所有分块。
    优先用元数据过滤删除（不依赖 chunk_count 是否准确）；
    失败时回退到按 id 前缀构造的方式。
    \"\"\"
    try:
        existing = self.collection.get(where={"doc_id": doc_id}, include=[])
        ids = existing.get("ids") or []
        if ids:
            self.collection.delete(ids=ids)
            return len(ids)
        return 0
    except Exception as exc:
        logger.warning("按 doc_id 删除向量失败，回退到 id 前缀方式: %s", exc)

    # 回退方案：doc_id + chunk_index 拼 ID
    if not chunk_count:
        return 0
    chunk_ids = [f"{doc_id}_chunk_{i}" for i in range(chunk_count)]
    self.collection.delete(ids=chunk_ids)
    return len(chunk_ids)
""", "核心代码 6-4　按 doc_id 删除向量（含回退方案）"),
("note", "为什么优先用元数据过滤？因为 `chunk_count` 存在数据库里，"
         "一旦写入过程中断（比如向量库写入到一半失败），数据库记录的分块数和向量库实际数量就会不一致。"
         "依赖计数删除会漏删；按 `doc_id` 过滤查询是**以实际数据为准**，更可靠。", "tip"),

("pagebreak",),

# ==================================================================== #
("h1", "缓存与降级设计", None),
# ==================================================================== #

("h2", "7.1 为什么需要缓存"),
("p", "每次问答都要调用 LLM，成本和延迟都不低。内部知识库的问答有很强的重复性"
      "（同一份文档被不同人反复问），缓存能直接省掉一次完整的 RAG + LLM 调用。"),

("h2", "7.2 缓存实现"),
("code", """
class CacheService:
    \"\"\"
    缓存服务：优先使用 Redis，不可用时回退到进程内内存缓存。
    \"\"\"

    def __init__(self):
        self.redis = None
        self.ttl = settings.CACHE_TTL
        try:
            import redis.asyncio as aioredis
            self.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception:
            pass                                   # 没有 Redis 客户端也不影响启动

    def _make_key(self, message: str) -> str:
        return f"cache:{hashlib.sha256(message.encode()).hexdigest()}"

    async def get(self, message: str) -> Optional[str]:
        key = self._make_key(message)
        if self.redis:
            try:
                cached = await self.redis.get(key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass                               # ★ 降级：异常不向上抛
        return _memory_cache.get(key)

    async def set(self, message: str, response: str):
        key = self._make_key(message)
        if self.redis:
            try:
                await self.redis.setex(key, self.ttl, json.dumps(response, ensure_ascii=False))
                return
            except Exception:
                pass                               # ★ 写失败也要能兜住
        _memory_cache[key] = response
""", "核心代码 7-1　Redis 缓存与内存降级"),

("h2", "7.3 降级的三个层次"),
("table",
 ["层次", "故障场景", "降级行为", "用户感知"],
 [
  ["缓存层", "Redis 连接失败 / 超时 / 命令报错", "静默切换到进程内 `_memory_cache` dict", "无感知，仅命中率下降"],
  ["缓存层", "Redis 写入异常", "写入内存缓存并正常返回结果", "无感知"],
  ["重排层", "Cross-Encoder 模型加载失败", "跳过重排，按向量检索原顺序取 Top-K", "答案相关性可能略降"],
  ["检索层", "向量库不可用 / 召回为空", "不注入上下文，回退为通用对话回答", "回答不再基于知识库"],
  ["工具层", "工具执行异常", "捕获异常转为可读文本交给 LLM 组织回答", "回答中说明工具调用失败"],
  ["意图层", "分类 LLM 调用失败", "降级为 `direct` 分支", "可能少走检索，但不会报错"],
  ["限流层", "slowapi 未安装", "禁用限流，服务正常启动", "无感知"],
  ["Embedding", "中文模型加载失败", "回退 Chroma 默认英文 embedding（会告警）", "中文检索质量下降"],
 ],
 [2.4, 4.6, 6.2, 3.4]),
("note", "这个表格很适合背下来回答「你的系统怎么做容错」。"
         "核心思想是：**关键链路（能返回答案）优先于最优效果（答案最准）**。"
         "缓存、重排、工具都是「增强项」，它们挂了不应该让用户拿不到回答。", "tip"),

("h2", "7.4 缓存键必须包含 user_id（安全修复）"),
("p", "原实现的缓存键只有消息内容，这会导致**跨用户信息泄露**："
      "用户 A 问「我的知识库有哪些文档」，用户 B 问同样的问题时，"
      "会直接命中 A 的答案。修复方式是把 `user_id` 纳入缓存键。"),
("code", """
# ✗ 原实现：跨用户串答案
cache_key = chat_request.message
cached_result = await cache.get(cache_key)

# ✓ 修复后：缓存键包含用户维度
cache_key = f"{user.id}:{chat_request.message}"
cached_result = await cache.get(cache_key)
""", "核心代码 7-2　缓存键的用户隔离修复"),

("h2", "7.5 缓存的语义边界（面试要主动澄清）"),
("p", "当前缓存用的是 **SHA-256 摘要做键 → 属于精确匹配缓存**，不是语义缓存。"
      "「如何配置 Redis」和「怎么配置 Redis」是两个不同的键，永远不会互相命中。"),
("table",
 ["类型", "原理", "优点", "缺点"],
 [
  ["精确匹配缓存（本项目）", "对原文做摘要作为 key", "实现简单、零误判、查询极快", "措辞稍有不同就无法命中"],
  ["语义缓存", "把 query 向量化，在缓存库中做相似度检索，超过阈值即命中", "表述不同也能命中，命中率显著更高", "有误判风险（阈值难调）、需要额外向量检索开销"],
 ],
 [3.6, 6.0, 4.2, 3.8]),
("note", "**主动澄清这一点是加分项，被追问出来是减分项。**"
         "建议说法：「我实现的是精确匹配缓存，用摘要做键。"
         "真正的语义缓存需要把 query 向量化后做相似度检索并设阈值，"
         "好处是措辞不同也能命中，代价是有误判风险和额外检索开销——这是我下一步的优化方向。」", "warn"),

("h2", "7.6 内存缓存的已知缺陷"),
("p", "作为降级方案的内存缓存是**无上限的 dict**，长期运行会持续增长。"
      "这是一个应当主动承认的缺陷，正确的改进方向是加 LRU 淘汰与容量上限："),
("code", """
from collections import OrderedDict

class LruMemoryCache:
    \"\"\"带容量上限的 LRU 内存缓存（改进方向示例）。\"\"\"
    def __init__(self, maxsize: int = 1000):
        self._data: "OrderedDict[str, str]" = OrderedDict()
        self.maxsize = maxsize

    def get(self, key):
        if key not in self._data:
            return None
        self._data.move_to_end(key)          # 命中即刷新热度
        return self._data[key]

    def set(self, key, value):
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        if len(self._data) > self.maxsize:
            self._data.popitem(last=False)   # 淘汰最久未使用
""", "核心代码 7-3　内存缓存的 LRU 改进方案"),
("note", "面试时主动说「我的内存缓存是无上限 dict，是个隐患，应该加 LRU」"
         "——**自己指出缺陷比被面试官发现更能体现工程判断力**。", "tip"),

("pagebreak",),

# ==================================================================== #
("h1", "安全设计", None),
# ==================================================================== #

("h2", "8.1 密码存储：bcrypt 加盐哈希"),
("code", """
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())
""", "核心代码 8-1　密码哈希与校验"),
("ul", [
    "**加盐**：`gensalt()` 每次生成不同盐值，相同密码的哈希结果也不同，抵御彩虹表",
    "**慢哈希**：bcrypt 设计上可调计算成本，暴力破解代价高（相比 MD5/SHA 直接哈希）",
    "**绝不存明文、绝不明文比较**：校验走 `checkpw`",
]),

("h2", "8.2 JWT 鉴权"),
("code", """
def create_token(user_id: str) -> str:
    # 使用 timezone-aware 的 UTC 时间：datetime.utcnow() 在 Python 3.12+ 已废弃
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire},
                      settings.SECRET_KEY, algorithm=settings.ALGORITHM)


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security),
                           db: AsyncSession = Depends(get_db)) -> User:
    user_id = decode_token(credentials.credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="无效的token")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user
""", "核心代码 8-2　JWT 签发与鉴权依赖"),
("h3", "已知边界（面试主动说明）"),
("table",
 ["议题", "当前实现", "风险与改进方向"],
 [
  ["Token 存储位置", "前端存 localStorage", "存在 XSS 窃取风险；更稳妥是 HttpOnly Cookie + CSRF 防护"],
  ["Token 过期处理", "过期需重新登录（30 分钟）", "生产环境应加 refresh token 机制，避免用户频繁重登"],
  ["Token 吊销", "无黑名单机制", "签发后无法主动失效；可引入 Redis 黑名单或改用短期 token + 刷新"],
  ["密钥强度", "从环境变量读取，默认值是占位符", "生产必须替换为强随机值，绝不能提交到仓库"],
 ],
 [3.0, 5.4, 9.2]),

("h2", "8.3 路径穿越防护"),
("p", "头像接口最初直接用拼接路径读取文件，攻击者可以通过 `../` 读取任意文件。"
      "修复方式是 `resolve()` 之后校验前缀："),
("code", """
@router.get("/avatar/{filename}")
async def get_avatar(filename: str):
    avatar_dir = Path(__file__).resolve().parent.parent.parent / "uploads" / "avatars"
    filepath = (avatar_dir / filename).resolve()

    # 防止 ../ 路径穿越读取任意文件
    if not str(filepath).startswith(str(avatar_dir.resolve())) or not filepath.exists():
        raise HTTPException(status_code=404, detail="头像不存在")
    return FileResponse(filepath)
""", "核心代码 8-3　路径穿越防护"),
("note", "更彻底的做法是在写入侧也不使用用户传入的文件名——"
         "本项目上传头像时用 `f\"{user.id}.{ext}\"` 命名，从根本上避免了恶意文件名。", "tip"),

("h2", "8.4 用户级数据隔离"),
("p", "知识库是多用户共用一个向量库，隔离靠元数据过滤；关系库靠查询条件。**两层都要做**："),
("code", """
# 向量库层：查询时带 user_id 过滤
params = {"query_texts": [query], "n_results": top_k}
if user_id:
    params["where"] = {"user_id": user_id}
results = self.collection.query(**params)

# 关系库层：所有查询都带 user_id 条件
result = await db.execute(
    select(Document).where(Document.id == doc_id, Document.user_id == user.id)
)
""", "核心代码 8-4　两层用户隔离"),
("note", "**这是软隔离**：数据物理上在同一个 collection / 同一张表里，靠应用层条件过滤。"
         "如果被问「怎么保证绝对安全」，应回答：「当前是应用层过滤，"
         "更严格的做法是每个用户独立 collection，或者用数据库行级权限（RLS）。」"
         "——诚实说明边界比硬撑更好。", "warn"),

("h2", "8.5 其他防护"),
("ul", [
    "**接口限流**：slowapi 按 IP 限制聊天接口 20 次/分钟，防止 API Key 被恶意刷",
    "**上传大小限制**：文档 5MB、头像 2MB，防止内存与存储被占满",
    "**上传格式白名单**：头像仅允许 png/jpg/jpeg/gif/webp",
    "**CORS 白名单**：只放行本地开发地址，不使用 `allow_origins=['*']`",
    "**密钥不入库**：`.env` 已在 `.gitignore` 中，仓库只提交 `.env.example` 占位",
    "**全局异常处理**：对外返回通用错误文案，详细堆栈只写日志，避免泄露内部结构",
]),

("h2", "8.6 密钥安全（重要提醒）"),
("note", "**如果任何真实 API Key 曾经被提交到公开仓库，必须立即到对应平台吊销并重新生成。**"
         "git 历史是不可变的——即使后来删除了文件或用新提交覆盖，"
         "旧密钥仍然可以从历史提交里被检出。清理历史需要 `git filter-repo` 或 BFG 工具，"
         "而且如果仓库已被 fork 或抓取，清理也无法保证密钥失效。**唯一可靠的做法是吊销密钥。**", "danger"),

("pagebreak",),

# ==================================================================== #
("h1", "工程实践与踩坑记录", None),
# ==================================================================== #

("h2", "9.1 完整踩坑清单"),
("p", "下表是本项目在完善过程中解决的**真实缺陷**。这些内容在面试里是很好的素材——"
      "它们证明你真的运行、调试过一个系统，而不只是把教程代码拼起来。"),
("table",
 ["#", "问题", "现象", "根因与修复"],
 [
  ["1", "ChatOpenAI 参数名失效", "构造 LLM 客户端时直接抛 `OpenAIError: Missing credentials`",
   "新版 langchain-openai 移除了 `openai_api_key` / `openai_api_base` 别名，必须用 `api_key` / `base_url`"],
  ["2", ".env 读取路径错误", "从项目根目录启动时读不到配置，API Key / SECRET_KEY 全部取默认值",
   "`env_file=\".env\"` 相对**当前工作目录**解析；改为相对 `config.py` 所在目录定位"],
  ["3", "分块依赖缺失", "`langchain_text_splitters` 缺失导致 RAG 层 import 失败，服务起不来",
   "该包在 langchain 1.x 已非默认安装；改为零依赖自实现递归分块"],
  ["4", "清洗破坏分块分隔符", "段落分隔符失效，分块质量差",
   "`\" \".join(text.split())` 把换行也压成空格；改为只压缩空白、保留换行结构"],
  ["5", "Chroma 维度不匹配", "读写全部报 `expecting embedding with dimension of 512, got 384`",
   "索引是 384 维默认英文模型；改为检测不一致时重建 collection"],
  ["6", "Chroma 不恢复 embedding function", "首次运行正常，**进程重启后必失败**",
   "打开已存在 collection 时回落 `DefaultEmbeddingFunction`；显式重挂 `_embedding_function`"],
  ["7", "Chroma 查询调用约定", "写入正常但 `query` 恒定失败",
   "只实现 `__call__` 不够，`query` 走 `embed_query(input=...)`；两个方法都要实现"],
  ["8", "HF 缓存目录不可写", "中文模型加载失败并**静默降级**为英文模型，检索质量骤降",
   "默认缓存目录不可写；配置阶段切换到项目内可写目录并关闭符号链接告警"],
  ["9", "slowapi 硬依赖", "缺包时 `app.api.routes` 导入失败，服务无法启动",
   "改为可选依赖，缺失时用空装饰器降级并打印告警"],
  ["10", "缓存跨用户串号", "不同用户询问相同问题会命中彼此答案",
   "缓存键只含 message；改为 `f\"{user_id}:{message}\"`"],
  ["11", "路径穿越", "头像接口可通过 `../` 读取目录外文件",
   "`resolve()` 后校验路径前缀；写入侧改用 user_id 命名"],
  ["12", "工具上下文并发串数据", "并发请求下工具读到其他用户的 user_id",
   "模块级全局变量在协程间共享；改用 `contextvars` 做请求级隔离"],
  ["13", "前端 SSE 分帧解析", "偶发 JSON 解析失败",
   "TCP 分片不保证帧完整，需用 `buffer = lines.pop()` 跨块拼接不完整行"],
  ["14", "Docker 部署白屏", "`docker compose up` 后页面空白",
   "Dockerfile 只拷贝了未构建的 `index.html`（引用 `.tsx`）；改为多阶段构建产出 `dist`"],
  ["15", "Nginx 缓冲攒批", "本地流式正常，上线后变一次性输出",
   "需设置响应头 `X-Accel-Buffering: no` 关闭 Nginx 缓冲"],
  ["16", "死代码与乱码", "多个从未被引用的 Agent 模块、中文注释乱码",
   "删除 `llm_agent.py` / `rag_agent.py` / `tool_agent.py` / `task.py`，修复文件编码"],
  ["17", "Redis / Celery 悬空配置", "requirements 装了 celery 但代码从未使用",
   "移除未使用依赖与配置项，避免误导"],
  ["18", "测试从未跑通", "`conftest.py` 用 `hashed_password=` 构造 User，字段名实际是 `password_hash`",
   "fixture 一旦使用就抛 TypeError；修正字段名并补全用例"],
 ],
 [0.9, 3.4, 5.2, 8.1]),

("h2", "9.2 从这些坑里能提炼出的经验"),
("ul", [
    "**静默降级是双刃剑**：Embedding 降级、重排降级保证了可用性，"
    "但如果没有告警，问题会被隐藏很久。所以降级路径必须有 WARNING 日志",
    "**「看起来像」不等于「就是」**：伪流式前端效果和真流式一模一样，但 TTFT 完全不同。"
    "评估一个实现要看关键指标，不能只看表面现象",
    "**状态污染是并发系统的头号敌人**：全局变量、共享可变对象、跨请求单例，"
    "任何一处疏忽都会变成「偶发串数据」的疑难 bug",
    "**依赖变化会静默破坏代码**：`openai_api_key` 参数名、`langchain_text_splitters` 包归属，"
    "都是版本升级后失效的——所以要**真的把项目跑起来并写测试**，而不是只写代码",
    "**测试的价值在于暴露假设错误**：`conftest.py` 的字段名错误说明这套测试从来没有真正执行过",
]),

("h2", "9.3 测试与质量保障"),
("p", "项目包含三层验证：pytest 单元/接口测试、不依赖 pytest 的结构自检脚本，"
      "以及需要真实 API Key 的 LLM 路径验证。"),
("table",
 ["验证方式", "覆盖内容", "运行命令"],
 [
  ["pytest", "鉴权保护、会话 CRUD、文档上传、知识库统计、路径穿越、分块边界、缓存降级、"
             "工具 schema、JWT 篡改、审查解析、协作回路",
   "`cd backend && pytest tests/ -v`"],
  ["结构自检（45 项）", "模块导入、分块规则、图路由与回路结构、缓存降级、工具错误处理、"
                       "向量写入/检索/删除闭环、**协作回路全流程的确定性验证**、"
                       "真实 PostgreSQL 下的完整 API 链路（注册→鉴权→会话→上传→统计→删除）",
   "`python verify_project.py`"],
  ["真实 LLM 验证（11 项）", "意图分类准确性、审查 Agent 的三种判定"
                            "（无证据编造→不通过 / 有证据一致→通过 / 资料不足但诚实说明→通过）、"
                            "完整图执行的轨迹完整性",
   "`python verify_llm_flow.py`"],
 ],
 [3.0, 8.6, 4.6]),
("p", "自检脚本的一个设计考虑：**不依赖 pytest**。"
      "本机曾在受限环境中缺少 pytest 与 aiosqlite，此时仍需要一个能真实验证系统的手段——"
      "于是用 sqlite / 真实 PostgreSQL + httpx ASGITransport 直接驱动应用。"),

("h3", "协作回路的确定性验证（第 9 节，7 项）"),
("p", "这是本项目验证体系里最值得说的一部分：**审查回路的行为不是靠「跑一次看看」验证的**，"
      "而是用注入式装置让每一轮的结果都成为可复现的输入。"
      "装置位于 `backend/tests/loop_harness.py`，与 pytest 用例共用同一份实现，避免重复维护。"),
("p", "做法是把 LLM 调用（意图分类 / 草稿生成 / 审查）替换为可控桩函数，"
      "而**真实的图结构、路由函数、判据与 finalize 逻辑全部保留**。"
      "于是「第几轮通过」「一直不通过」都能被精确构造出来："),
("table",
 ["被验证的行为", "断言"],
 [
  ["首轮通过", "生成 1 次、审查 1 次，不触发多余重做（不做无用功）"],
  ["第 2 轮通过", "Worker **确实被再次调用** —— 直接证明回路的边被走通了"],
  ["每轮重做", "携带上一轮的审查意见（反馈真的回传给了生成 Agent）"],
  ["永不通过", "**恰好 3 轮后停止**（`MAX_REVISIONS` 生效，不会无限循环）"],
  ["达上限仍不通过", "**输出诚实拒答，且原草稿内容绝不出现在输出里** ★核心保证"],
  ["全流程", "`agent_trace` 按执行顺序累加（reducer 生效，轨迹未被覆盖）"],
  ["修订过程", "回到原分支而非重新分类（`supervisor` 仅被调用 1 次）"],
 ],
 [4.2, 12.4]),
("note", "**为什么值得这么做**：如果只靠「跑一次真实 LLM 看看结果」，"
         "「第 3 轮才通过」这种分支几乎不可能被稳定复现，"
         "而「达上限必须拒答」是**安全保证**——它不能被「碰巧没触发」掩盖过去。"
         "把它变成确定性测试，才能保证后续改动不会悄悄破坏它。", "tip"),
("h3", "测试实现中踩到的坑（可作为「替换图节点行为」的参考）"),
("ol", [
    "**替换实例属性无效**：`_build_graph()` 在 `__init__` 中编译图时，"
    "节点位置就**持有了方法对象的引用**；之后再给实例赋同名属性，"
    "图仍会调用原来的方法（实例属性遮蔽对已持有的引用无效）",
    "**即使改成类级替换，提前还原也会失效**：`_generate_draft` 是 Worker 在"
    "**运行期**通过 `self.` 查找的，一旦在 `__init__` 之后立刻还原类属性，"
    "Worker 就会调回真实 LLM——实测表现为「Worker 生成 0 次」且输出变成真实模型的回答",
    "**正确做法**：先替换类方法 → 再创建 agent（图用桩函数编译）→ "
    "贯穿整次图执行期间保持替换 → 执行结束后统一还原",
]),
("note", "这个约束对生产也有意义：**运行期替换图节点行为必须走工厂或改图本身**，"
         "不能靠给实例打补丁——这决定了「按租户 / 环境动态替换某个 Agent」这类需求该怎么实现。", "info"),
("code", """
# verify_project.py 的核心思路：用 ASGITransport 在进程内驱动 FastAPI 应用，
# 覆盖真实的请求-响应链路，不依赖外部测试框架。
from httpx import ASGITransport, AsyncClient
from app.core.app import app
from app.core.database import Base, get_db

engine = create_async_engine(PG_URL)
async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.drop_all)
    await conn.run_sync(Base.metadata.create_all)

async def override_get_db():
    async with Session() as s:
        yield s

app.dependency_overrides[get_db] = override_get_db
transport = ASGITransport(app=app)
async with AsyncClient(transport=transport, base_url="http://test") as ac:
    r = await ac.post("/api/v1/auth/register", json={...})
    # ...完整链路断言
""", "核心代码 9-1　端到端自检的实现方式"),

("h2", "9.4 部署架构"),
("code", """
docker-compose.yml
┌───────────────────────────────────────────────────────────────┐
│  backend（多阶段构建镜像）                                      │
│    阶段1: node:20-slim  → npm ci && npm run build（产出 dist）  │
│    阶段2: python:3.11-slim → pip install + 预下载向量模型        │
│          拷贝 backend/ 与 前端的 dist/                          │
│    HEALTHCHECK: curl -fsS http://localhost:8080/health         │
│    端口 8080                                                    │
├───────────────────────────────────────────────────────────────┤
│  postgres:16-alpine   healthcheck: pg_isready -U codepilot     │
│    volume: pg_data（数据持久化）                                │
├───────────────────────────────────────────────────────────────┤
│  redis:7-alpine       healthcheck: redis-cli ping              │
│    volume: redis_data                                          │
├───────────────────────────────────────────────────────────────┤
│  volumes: chroma_data（向量库）/ hf_cache（模型缓存）/ uploads   │
└───────────────────────────────────────────────────────────────┘
""", "图 9-1　容器编排结构"),
("p", "多阶段构建的价值：镜像里**不包含** node_modules 与前端源码，只保留构建产物；"
      "同时在构建阶段预下载向量模型，避免容器首次启动时因下载模型而超时或降级。"),

("pagebreak",),

# ==================================================================== #
("h1", "面试问题汇总", None),
# ==================================================================== #

("h2", "10.1 项目与架构类"),
("qa", "介绍一下你的多 Agent 架构，为什么用 Supervisor-Worker 而不是单 Agent？", [
    "**先讲问题**：单 Agent 要同时做检索、工具调用、通用对话，所有工具描述与行为指令塞进同一个 system prompt，"
    "导致 prompt 膨胀、意图混淆、行为不可控。",
    "**再讲方案**：Supervisor 只做意图分类，输出 rag / tool / direct 三选一。"
    "分类 prompt 很短，`max_tokens=16`，成本与延迟都低；每个 Worker 的 prompt 只关注自己那类任务。",
    "**再讲实现**：用 LangGraph 的 `StateGraph`，`add_conditional_edges` 按 `next_worker` 路由；"
    "节点之间通过共享 `AgentState`（TypedDict，`total=False`）传会话历史与执行轨迹；"
    "每个 Worker 用 `add_edge(..., \"__end__\")` 独立收敛。",
    "**主动补边界**：当前是单跳路由，不支持 Worker 链式串联，这是可以改进的地方。",
], [
    "意图分类为什么用 LLM 而不是规则？——规则难覆盖自然语言多样性；但我会补充：高 QPS 下可以换小模型或加分类缓存。",
    "分类错了怎么办？——当前兜底到 direct；RAG 分支在召回为空时也会降级为通用回答。",
    "Supervisor 会不会成为瓶颈？——分类本身很轻（16 token），如果需要可以并行分类或换更小的模型。",
]),

("qa", "你的流式输出是怎么实现的？", [
    "**先说数据流**：FastAPI 返回 `StreamingResponse`，`media_type=\"text/event-stream\"`，"
    "并设置 `X-Accel-Buffering: no` 关闭 Nginx 缓冲。",
    "**再说关键设计**：Worker 节点内部调用 LLM 的 `astream()`，"
    "把**异步生成器作为节点出参**放进 state，节点立即返回；"
    "路由层从最终 state 取出生成器逐 token 透传。这样 TTFT 只等于首个 token 的产出时间。",
    "**先讲事件协议**：FastAPI 返回 `StreamingResponse`，`media_type=\"text/event-stream\"`，"
    "并设置 `X-Accel-Buffering: no` 关闭 Nginx 缓冲。自定义了 "
    "token / trace / conv_id / error / done 五类帧，因为前端除了内容还要拿执行轨迹和会话 ID。",
    "**再讲一个真实的取舍**（这是最能体现工程判断的部分）：我最初是 LLM 层 `astream` "
    "真逐 token 透传，首字延迟约 1.46 秒。但加入审查 Agent 后，"
    "为了保证审查能拦住**整段**内容，必须改成「完整生成 → 审查 → 再下发」，"
    "**首字延迟上升到约 2.42 秒（+66%）**。",
    "**明确说出结论**：**全文审查与真流式在单次请求内不可兼得**。"
    "我的选择是可靠性优先——知识库问答里编造的数字会让用户拿去做错误决策。"
    "如果两者都要，可以改成**分段审查**（生成一段、审查一段、通过即下发），"
    "代价是无法发现跨段落的不一致。",
], [
    "为什么不用 WebSocket？——SSE 单向足够，基于 HTTP 实现和运维更简单；WebSocket 适合双向场景如协同编辑。",
    "前端为什么不用 EventSource？——它只支持 GET 且不能自定义请求头，我们需要 POST + JWT。",
    "流式过程中怎么存数据库？——在生成前累积完整定稿，落库后统一下发。",
    "那你的「流式」还有什么意义？——它现在提供的是**增量渲染体验**（打字机节奏与轨迹可视化），"
    "而不是降低首字延迟。这一点我会如实说明，不把它包装成性能优化。",
]),

("qa", "你提到了审查 Agent，它具体怎么防止模型编造？", [
    "**先说清问题**：生成 Agent 有两类无法自我发现的问题。一是**无依据断言**——"
    "检索为空或工具没返回数据时，模型仍会给出具体数字与文件名，而且**不抛异常、日志正常**；"
    "二是**答非所问**。同一个模型在同一次生成里已经走完那条推理路径，无法可靠地审查自己。",
    "**再说做法**：把「生成」和「审查」拆成**两次独立的 LLM 调用**。"
    "Verifier 只看 **问题 + 证据 + 草稿** 三样东西做判定，不参与生成过程，"
    "因此不受生成路径的思维定势影响。",
    "**判定原则**：① 草稿中每个具体事实（数字、文件名、配置项、API 名）"
    "都必须能在【参考资料】里找到出处；② 必须正面回答用户问题，多部分问题不能只答一半；"
    "③ **资料确实不足时，明确说明「没有相关信息」应判定为通过**——"
    "诚实拒答是合格答案，用编造内容掩盖资料不足才是不通过。",
    "**然后讲协作回路**：审查不通过时，Verifier 的 `feedback` 会写入 state，"
    "并在下一轮注入生成用的 system prompt，同时 `revise` 节点把控制流送回**原来那条分支**"
    "（而不是重新分类）。上限 3 轮。**这是本项目里 Agent 之间真实协作的地方**——"
    "有反馈、有回路，不是各跑各的。",
    "**最后讲最终保证**：连续 3 轮未通过时，`finalize` 节点输出的是**诚实拒答**，"
    "而不是那份可能编造的草稿。这一点在验证脚本里有专门断言："
    "「不通过时原草稿内容绝不能出现在输出里」。",
], [
    "审查不会拖慢响应吗？——会，而且我实测过：**首字延迟从 1.46s 增加到 2.42s（+66%）**。"
    "这是为可靠性付的代价，我认为在知识库问答场景值这个价。",
    "审查 Agent 自己会不会判错？——会。它也是一次 LLM 判定，同样可能误判。"
    "它做不到 100% 正确，能做到的是**把「无依据断言」这类高发错误拦在交付之前**，"
    "并且把每轮判定结论留在 `agent_trace` 里，让出错**可观测**。",
    "审查结果解析失败怎么办？——采用 **fail-open**（放行），并在 trace 里显式标注"
    "「解析失败（放行）」。理由是审查是增强环节，不应因为它自身出错就让用户拿不到回答；"
    "但必须标记出来，避免问题被隐藏。",
    "为什么修订要回到原分支而不是重新分类？——因为问题没变，变的只是答案质量。"
    "重新分类会引入不必要的抖动，还可能把本来正确的路由改错。",
    "为什么用 reducer 累加 agent_trace？——LangGraph 对同名字段默认是「后写覆盖先写」，"
    "不加 reducer 的话每个节点返回的 trace 会把前面的冲掉，最终只剩最后一行，"
    "「可观测性」就形同虚设。用 `Annotated[List[str], operator.add]` 才能按执行顺序累加。",
]),

("pagebreak",),
("h2", "10.2 RAG 类（出现频率最高）"),
("qa", "为什么要做重排？只用向量检索不行吗？", [
    "**本质区别**：向量检索是**双塔（bi-encoder）**结构，query 和 document 分别独立编码后算余弦相似度，"
    "两侧在编码时互相看不到对方，无法建模词级交互；"
    "**Cross-Encoder** 把 (query, document) 拼接后送进同一个模型，注意力机制能捕捉交互，精度显著更高。",
    "**代价**：Cross-Encoder 必须对每个候选实时前向推理，无法预建索引，全库打分时间不可接受。",
    "**所以两阶段**：双塔快速召回 Top-10，再用 Cross-Encoder 精排保留 Top-5。"
    "一句话总结：**双塔负责快，交叉编码器负责准**。",
], [
    "Top-10 / Top-5 怎么定的？——初始按经验值；更好的做法是用评测集调参（召回率与上下文长度的权衡）。",
    "512/50 的分块参数怎么定的？——按经验与模型上下文长度；应该用评测集验证最优值。",
    "怎么评估检索效果？——自建问答测试集，统计 Hit@K / Recall@K / MRR（这是当前项目的改进方向）。",
    "召回为空怎么办？——不注入上下文，回退通用回答，并在 Agent 轨迹里标注。",
]),

("qa", "你的 Embedding 用的什么模型？为什么？", [
    "用的是 **`BAAI/bge-small-zh-v1.5`**，512 维，中文语料训练，体积约 95MB。",
    "**为什么不用默认的**：Chroma 不指定 embedding function 时会用内置的 `all-MiniLM-L6-v2`，"
    "那是**英文**语料训练的 384 维模型。我的文档全是中文，用它检索会明显失真。",
    "**踩的坑**（这个回答很能体现深度）：Chroma 在打开**已存在**的 collection 时不会恢复 "
    "embedding function，会回落到默认的 384 维模型——表现为第一次运行正常、进程重启后必定报维度错误。"
    "解决办法是打开后显式把 `_embedding_function` 重新挂回去。",
    "另外自定义 embedding function 只实现 `__call__` 不够，`query()` 走的是 `embed_query(input=...)`，"
    "只实现 `__call__` 会导致「写入正常但检索恒定失败」。",
], [
    "bge 和 m3e、text2vec 怎么选？——主要看中文语义检索榜单（C-MTEB）表现、模型体积与推理速度的权衡。",
    "为什么不调用 API 做 embedding？——本地模型无网络依赖、无调用成本；"
    "但如果追求效果可以换 DashScope 的 `text-embedding-v3`（代码里已预留 provider 切换）。",
]),

("qa", "你的 RAG 怎么保证用户之间数据不串？", [
    "**两层隔离**：向量库层在写入时把 `user_id` 写进 metadata，查询时用 `where={\"user_id\": ...}` 过滤；"
    "关系库层所有查询都带 `user_id` 条件。",
    "**主动说明边界**：这是**应用层软隔离**，数据物理上在同一 collection 里。"
    "如果要求更强的隔离，应该每个用户独立 collection，或者使用数据库行级权限（RLS）。",
], [
    "如果在 where 过滤上写错了会怎样？——会串数据；所以我在测试里显式断言了检索结果的 user_id 必须匹配。",
]),

("pagebreak",),
("h2", "10.3 Function Calling 类"),
("qa", "你的工具调用是怎么实现的？工具需要数据库会话，怎么传进去？", [
    "**调用链路**：用 `llm.bind_tools(ALL_TOOLS)` 让模型返回结构化 `tool_calls`，"
    "遍历执行后把结果作为 `ToolMessage`（带 `tool_call_id`）回填到消息列表，再让模型基于结果生成最终回答，最多 3 轮。",
    "**上下文传递**（关键是这个）：工具需要 `AsyncSession` 和 `user_id`，"
    "但它们不能放进 JSON Schema。**我没有用模块级全局变量——那样在并发下会串用户数据**，"
    "而是用 `contextvars.ContextVar`：它对每个 asyncio Task 独立，"
    "并发的 SSE 请求各自持有自己的上下文。",
    "**降级**：工具执行异常会被捕获并转成可读文本交给 LLM 组织回答，不会让整个请求 500。",
], [
    "为什么决策阶段不用流式？——流式返回的 tool_calls 是分片到达、需要手动拼接，复杂且易错；"
    "工具决策输出很短，非流式延迟可接受，而最终回答仍然是流式的。",
    "怎么防止无限循环调用工具？——设了 `MAX_TOOL_ROUNDS = 3` 上限。",
    "怎么保证模型选对工具？——工具的 docstring 就是给模型看的描述，我在里面写明了使用场景；"
    "参数用类型注解，会转成 JSON Schema。",
]),

("h2", "10.4 缓存与降级类"),
("qa", "Redis 挂了你的服务会怎样？", [
    "**不会挂**。缓存层所有 Redis 操作都包在 try/except 里，异常时静默降级到进程内的内存缓存 dict，"
    "读写都能继续，只是命中率下降（内存缓存不跨进程、重启即失效）。",
    "**设计思想**：缓存是「增强项」而不是「必需项」。核心原则是"
    "**关键链路（能返回答案）优先于最优效果（答案最准）**——"
    "缓存、重排、工具调用挂掉都不应该让用户拿不到回答。",
], [
    "内存缓存有什么问题？——当前是无上限 dict，是内存泄漏隐患，应该加 LRU 与容量上限（我能写出实现）。",
    "你的缓存是语义缓存吗？——**不是**，是 SHA-256 摘要的精确匹配缓存。"
    "语义缓存需要把 query 向量化后做相似度检索并设阈值，命中率更高但有误判风险。",
    "缓存键包含用户维度吗？——包含。`f\"{user_id}:{message}\"`。"
    "最初只用了 message，会导致不同用户命中彼此的答案，这是个安全 bug，已经修掉了。",
]),

("qa", "你的系统还做了哪些容错设计？", [
    "可以直接把降级表格讲出来，体现系统性思考：",
    "**缓存层**：Redis 不可用 → 内存缓存；写入异常 → 兜住不影响返回。",
    "**重排层**：Cross-Encoder 加载失败 → 跳过重排按向量顺序取 Top-K。",
    "**检索层**：召回为空 → 不注入上下文，回退通用回答。",
    "**工具层**：执行异常 → 转可读文本交给 LLM 说明。",
    "**意图层**：分类失败 → 降级 direct。",
    "**限流层**：slowapi 缺失 → 禁用限流但服务正常启动。",
], [
    "降级会不会掩盖问题？——会，所以每条降级路径都必须打 WARNING 日志。"
    "比如 Embedding 降级如果不告警，检索质量下降会被隐藏很久。",
]),

("pagebreak",),
("h2", "10.5 数据库与后端类"),
("qa", "为什么用异步 ORM？异步在这里的价值是什么？", [
    "聊天接口是典型的 **IO 密集型**：等待 LLM 生成、等待数据库查询、等待向量库检索，"
    "CPU 大部分时间在等 IO。同步模型下每个请求占一个线程，线程数就是并发上限；"
    "异步在等待期间释放事件循环去处理其他请求，同样资源能支撑更高并发。",
    "**具体实现**：`create_async_engine` + `asyncpg` 驱动 + `async_sessionmaker`，"
    "通过 FastAPI 的 `Depends(get_db)` 注入会话。",
    "**注意点**：`expire_on_commit=False` 是必须的——默认 True 会在提交后使对象属性过期，"
    "后续访问属性触发隐式 IO，在异步上下文里会报 `MissingGreenlet`。",
], [
    "异步里能写阻塞代码吗？——不能。所以我把 Chroma 查询和 Cross-Encoder 推理"
    "都用 `asyncio.to_thread` 丢进线程池，否则会阻塞事件循环、把并发请求串行化。",
    "SQLAlchemy 2.0 相比 1.x 有什么变化？——引入 `Mapped` / `mapped_column` 类型注解式声明，"
    "类型检查更友好；异步支持更完善。",
]),

("qa", "你的接口怎么鉴权？有安全问题吗？", [
    "**实现**：登录后用 python-jose 签发 HS256 的 JWT（`sub` 存 user_id，带 `exp`），"
    "密钥从环境变量读取；FastAPI 用 `HTTPBearer` + `Depends(get_current_user)` 做依赖注入鉴权，"
    "在依赖里查库确认用户存在。密码用 bcrypt 加盐哈希存储。",
    "**主动说风险**：token 存在前端 localStorage，有 XSS 窃取风险，"
    "更稳妥的是 HttpOnly Cookie + CSRF 防护；另外没有 refresh token 与吊销机制，"
    "生产环境应该补上。",
], [
    "JWT 和 Session 的区别？——JWT 无状态、服务端不存会话、便于水平扩展；"
    "但无法主动失效，且 payload 是明文 Base64（不能放敏感信息）。",
    "怎么防止 token 被盗用？——短期有效期 + refresh token + 绑定设备指纹，必要时加黑名单。",
]),

("h2", "10.6 可能的现场编码题"),
("table",
 ["题目", "考察点", "作答要点"],
 [
  ["手写一个 LRU 缓存", "数据结构（哈希表 + 双向链表 / OrderedDict）", "get 时 move_to_end，set 超容量时 popitem(last=False)"],
  ["实现递归分块函数", "字符串处理与边界", "按分隔符优先级递归；处理超长单段；加 overlap"],
  ["两个有序数组合并 / 双指针", "基础算法", "见 LeetCode 相关题"],
  ["写一个带 TTL 的内存缓存装饰器", "装饰器 + 异步", "注意 async 函数的装饰器要用 async wrapper"],
  ["把一段同步代码改成异步", "并发理解", "识别阻塞 IO，用 to_thread 或改异步库"],
  ["查询某用户的所有会话及消息条数", "SQL / ORM", "GROUP BY + COUNT，或用 selectinload 避免 N+1"],
 ],
 [4.6, 5.0, 7.0]),

("pagebreak",),

# ==================================================================== #
("h1", "附录", None),
# ==================================================================== #

("h2", "A.1 项目启动步骤"),
("h3", "Docker Compose（推荐）"),
("code", """
cp backend/.env.example backend/.env
# 编辑 backend/.env，至少填入 DEEPSEEK_API_KEY 与 SECRET_KEY
docker compose up -d --build
# 访问 http://localhost:8080
""", "启动命令"),
("h3", "本地开发"),
("code", """
# 后端
cd backend
pip install -r requirements.txt
cp .env.example .env          # 填入 DEEPSEEK_API_KEY
uvicorn app.core.app:app --reload --port 8080

# 前端（另开终端）
cd frontend
npm install
npm run dev                   # http://localhost:5173
""", "本地开发命令"),
("h3", "无 PostgreSQL 时的快速体验"),
("code", """
# 把 backend/.env 里的 DATABASE_URL 改成 SQLite
DATABASE_URL=sqlite+aiosqlite:///./codepilot.db
""", "SQLite 配置"),

("h2", "A.2 关键配置项说明"),
("table",
 ["配置项", "默认值", "说明"],
 [
  ["`DATABASE_URL`", "postgresql+asyncpg://...", "数据库连接串，可切 SQLite 用于快速体验"],
  ["`REDIS_URL`", "redis://localhost:6379/0", "Redis 地址，不可用时自动降级内存缓存"],
  ["`CACHE_TTL`", "300", "问答缓存有效期（秒）"],
  ["`DEEPSEEK_API_KEY`", "—", "**必填**，大模型 API Key"],
  ["`EMBEDDING_PROVIDER`", "local", "`local`（本地模型）或 `dashscope`（阿里云）"],
  ["`EMBEDDING_MODEL`", "BAAI/bge-small-zh-v1.5", "中文向量模型，**不要用英文模型**"],
  ["`CHUNK_SIZE` / `CHUNK_OVERLAP`", "512 / 50", "分块长度与重叠"],
  ["`RETRIEVE_TOP_K` / `RERANK_TOP_K`", "10 / 5", "向量召回数与重排保留数"],
  ["`SECRET_KEY`", "change-me-in-production", "**生产必须替换**，用于 JWT 签名"],
  ["`ACCESS_TOKEN_EXPIRE_MINUTES`", "30", "Token 有效期"],
  ["`CHAT_RATE_LIMIT`", "20/minute", "聊天接口限流"],
  ["`HF_ENDPOINT`", "https://hf-mirror.com", "国内下载模型用的镜像"],
 ],
 [5.4, 4.4, 6.8]),

("h2", "A.3 API 接口清单"),
("table",
 ["方法", "路径", "说明", "是否需要鉴权"],
 [
  ["POST", "`/api/v1/auth/register`", "注册并返回 token", "否"],
  ["POST", "`/api/v1/auth/login`", "登录并返回 token", "否"],
  ["GET", "`/api/v1/auth/me`", "获取当前用户", "是"],
  ["POST", "`/api/v1/chat`", "同步聊天（带缓存）", "是"],
  ["POST", "`/api/v1/chat/stream`", "**SSE 流式聊天**", "是"],
  ["GET", "`/api/v1/conversations`", "会话列表", "是"],
  ["POST", "`/api/v1/conversations`", "新建会话", "是"],
  ["DELETE", "`/api/v1/conversations/{id}`", "删除会话及其消息", "是"],
  ["GET", "`/api/v1/conversations/{id}/history`", "会话历史", "是"],
  ["POST", "`/api/v1/documents`", "上传文档（自动分块向量化）", "是"],
  ["GET", "`/api/v1/documents`", "文档列表", "是"],
  ["DELETE", "`/api/v1/documents/{id}`", "删除文档并清理向量", "是"],
  ["GET", "`/api/v1/documents/stats`", "知识库统计（含向量库一致性）", "是"],
  ["GET", "`/api/v1/profile`", "获取个人信息", "是"],
  ["PUT", "`/api/v1/profile`", "更新个人信息", "是"],
  ["POST", "`/api/v1/upload-avatar`", "上传头像", "是"],
  ["GET", "`/api/v1/avatar/{filename}`", "获取头像文件", "否"],
  ["GET", "`/health`", "健康检查（含依赖状态）", "否"],
 ],
 [1.9, 6.0, 5.4, 3.3]),
("p", "在线接口文档（OpenAPI / Swagger）：启动后访问 `http://localhost:8080/docs`。"),

("h2", "A.4 项目结构"),
("code", """
CodePilot/
├── backend/
│   ├── app/
│   │   ├── agents/supervisor.py     # LangGraph 编排 + 流式生成 + Function Calling
│   │   ├── api/
│   │   │   ├── auth.py              # 注册 / 登录 / me
│   │   │   └── routes.py            # 聊天 / 会话 / 知识库 / 个人中心
│   │   ├── core/
│   │   │   ├── app.py               # 应用装配、中间件、异常处理、前端托管
│   │   │   ├── config.py            # 配置（含 .env 定位与 HF 缓存兜底）
│   │   │   ├── auth.py              # bcrypt + JWT 鉴权
│   │   │   ├── database.py          # 异步引擎与会话
│   │   │   └── logging.py           # 日志初始化
│   │   ├── models/                  # SQLAlchemy 模型（users / conversations / chat_history / documents）
│   │   ├── rag/
│   │   │   ├── embeddings.py        # 中文 embedding 服务（单例 + 降级 + Chroma 适配）
│   │   │   ├── processor.py         # 零依赖递归分块
│   │   │   ├── retriever.py         # Chroma 访问层（单例 + 维度自愈）
│   │   │   ├── reranker.py          # Cross-Encoder 重排
│   │   │   └── tools.py             # Function Calling 工具集 + contextvars 上下文
│   │   └── services/cache.py        # Redis 缓存 + 内存降级
│   ├── tests/                       # pytest 用例
│   ├── pytest.ini
│   └── requirements.txt
├── frontend/src/
│   ├── components/                  # ChatMessage / ChatInput / AgentTrace
│   ├── pages/                       # Chat / KnowledgeBase / History / PersonalCenter / Login
│   └── services/api.ts              # axios 客户端 + SSE 流解析
├── verify_project.py                # 端到端自检脚本
├── Dockerfile                       # 多阶段构建
├── docker-compose.yml               # backend + postgres + redis（+ frontend 可选）
└── README.md
""", "项目目录结构"),

("h2", "A.5 面试前的速查卡"),
("note", "**打印这一页带进面试（或在面试前 5 分钟过一遍）**", "info"),
("h3", "三个必须能脱口而出的技术点"),
("ol", [
    "**双塔召回 + Cross-Encoder 重排**：双塔把 query 和 doc 独立编码，快但精度有限；"
    "Cross-Encoder 拼接后过同一模型，准但慢。所以 Top-10 召回 → Top-5 重排。",
    "**真流式 vs 伪流式**：伪流式是整段生成后再逐字吐，TTFT 没改善；"
    "真流式是在 LLM 层 `astream` 逐 token 透传。",
    "**contextvars 而非全局变量传工具上下文**：全局变量在并发协程间共享会串用户数据。",
]),
("h3", "三个必须主动承认的边界"),
("ol", [
    "缓存是**精确匹配**，不是语义缓存",
    "知识库隔离是**应用层软隔离**，不是物理隔离",
    "内存缓存**无上限**，应加 LRU",
]),
("h3", "一句话介绍项目（背下来）"),
("p", "「我做了一个研发知识助手，后端用 FastAPI，用 LangGraph 做了一套 Supervisor-Worker 的意图路由，"
      "前面挂一条 RAG 链路——文档分块向量化之后先向量召回再用 Cross-Encoder 重排；"
      "工具调用用的是真实的 function calling，有四个查文档和统计的工具；"
      "输出走 SSE 逐 token 流式；数据层是 PostgreSQL 加 Redis，Redis 挂了会自动降级到内存缓存。"
      "前端是 React + TypeScript，能看到 Agent 的执行轨迹。」"),
]
