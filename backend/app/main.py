from fastapi import FastAPI
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.assessment_operations import router as assessment_operations_router
from app.api.assessment_repetition import router as repetition_router
from app.api.assessment_session import router as assessment_router
from app.api.auth import router as auth_router
from app.api.teacher_api import router as teacher_router
from app.api.teacher_assessment import router as teacher_assessment_router
from app.api.teacher_llm_provider import router as teacher_llm_provider_router
from app.api.teacher_review import router as teacher_review_router
from app.api.teacher_scoring import router as teacher_scoring_router

app = FastAPI(title="VNext Assessment API")
app.include_router(auth_router)
app.include_router(teacher_router)
app.include_router(teacher_llm_provider_router)
app.include_router(teacher_scoring_router)
app.include_router(teacher_review_router)
app.include_router(teacher_assessment_router)
app.include_router(assessment_router)
app.include_router(assessment_operations_router)
app.include_router(repetition_router)


@app.exception_handler(RequestValidationError)
async def safe_validation_error(request, exc):
    if request.url.path.startswith(("/teacher/", "/auth/", "/admin/")):
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})
    return await request_validation_exception_handler(request, exc)


@app.middleware("http")
async def auth_response_cache_control(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/auth/", "/admin/users")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only; this endpoint does not check database readiness."""
    return {"status": "ok"}
