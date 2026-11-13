from fastapi import FastAPI

app = FastAPI(title="VNext Assessment API")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only; this endpoint does not check database readiness."""
    return {"status": "ok"}
