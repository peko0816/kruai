"""Version 1 of the API (PRD section 10)."""

from fastapi import APIRouter

from app.api.v1 import auth

#: Mounted by app.main at /api/v1. Routers are collected here rather than in
#: main so that adding an endpoint group touches one file.
router = APIRouter()
router.include_router(auth.router)

__all__ = ["router"]
