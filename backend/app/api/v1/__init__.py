"""Version 1 of the API (PRD section 10)."""

from fastapi import APIRouter

from app.api.v1 import admin, attempts, auth, courses, lessons, me, review

#: Mounted by app.main at /api/v1. Routers are collected here rather than in
#: main so that adding an endpoint group touches one file.
router = APIRouter()
router.include_router(auth.router)
router.include_router(courses.router)
router.include_router(lessons.router)
router.include_router(attempts.router)
router.include_router(review.router)
router.include_router(me.router)
router.include_router(admin.router)

__all__ = ["router"]
