from fastapi import FastAPI

from app.teacher_api import router as teacher_router

app = FastAPI(title="VNext Assessment API")
app.include_router(teacher_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only; this endpoint does not check database readiness."""
    return {"status": "ok"}
