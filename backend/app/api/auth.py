from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.auth import hash_password, verify_password, create_token, get_current_user
from app.models.user import User

router = APIRouter()


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    user_id: str
    username: str


class UserResponse(BaseModel):
    user_id: str
    username: str


@router.post("/register", response_model=TokenResponse)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == req.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="用户名已存在")

    user = User(username=req.username, password_hash=hash_password(req.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_token(user.id)
    return TokenResponse(token=token, user_id=user.id, username=user.username)


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_token(user.id)
    return TokenResponse(token=token, user_id=user.id, username=user.username)


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    return UserResponse(user_id=user.id, username=user.username)
