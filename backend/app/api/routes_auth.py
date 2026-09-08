"""
Kimlik Doğrulama Uçları
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..core.crypto import new_salt
from ..core.db import get_db
from ..core.security import create_access_token, hash_password, verify_password
from ..models import User
from ..schemas import LoginIn, RegisterIn, TokenOut
from .deps import current_user

router = APIRouter(prefix="/api/auth", tags=["Kimlik"])


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, db: Session = Depends(get_db)) -> TokenOut:
    """Yeni hesap oluşturur ve doğrudan oturum açar."""
    email = payload.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Bu e-posta zaten kayıtlı.")

    try:
        user = User(
            email=email,
            password_hash=hash_password(payload.password),
            vault_salt=new_salt(),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenOut(
        access_token=create_access_token(user.id, user.email),
        email=user.email, user_id=user.id,
    )


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    email = payload.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "E-posta veya parola hatalı.")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Hesap devre dışı.")
    return TokenOut(
        access_token=create_access_token(user.id, user.email),
        email=user.email, user_id=user.id,
    )


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }
