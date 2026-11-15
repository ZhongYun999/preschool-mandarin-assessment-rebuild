from fastapi import FastAPI

from app.api.teacher_api import router as teacher_router
from app.api.teacher_review import router as teacher_review_router
from app.api.teacher_scoring import router as teacher_scoring_router

app = FastAPI(title="VNext Assessment API")
app.include_router(teacher_router)
app.include_router(teacher_scoring_router)
app.include_router(teacher_review_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only; this endpoint does not check database readiness."""
    return {"status": "ok"}
