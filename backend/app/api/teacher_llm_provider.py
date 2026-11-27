"""Teacher management of global LLM credentials and configuration."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.teacher_api import get_db
from app.api.teacher_auth import require_teacher
from app.evaluation import provider_config as service
from app.evaluation.provider_models import ProviderCredential


class _SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"detail": "Invalid request"})

        return wrapped


router = APIRouter(
    prefix="/teacher/llm-provider",
    dependencies=[Depends(require_teacher)],
    route_class=_SecretSafeRoute,
)
db_dependency = Depends(get_db)


class ConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    model: str
    region: str | None = None
    workspace: str | None = None
    api_key: SecretStr | None = None
    credential_id: UUID | None = None


def _project(session, config):
    if config is None:
        return {"configured": False}
    credential = session.get(ProviderCredential, config.credential_id)
    try:
        available = bool(
            credential and credential.provider == config.provider and service._key(credential)
        )
    except service.CredentialUnavailable:
        available = False
    return {
        "configured": True,
        "available": available,
        "provider": config.provider,
        "model": config.model,
        "revision": config.revision,
        "config_id": str(config.config_id),
        "credential_id": str(config.credential_id),
        "credential_version": credential.version if credential else None,
        "region": config.region,
        "workspace": config.workspace,
    }


@router.get("")
def get_config(session: Session = db_dependency):
    return _project(session, service.current(session))


@router.put("")
def save_config(body: ConfigRequest, session: Session = db_dependency):
    try:
        with session.begin():
            config = service.save(
                session,
                provider=body.provider,
                model=body.model,
                region=body.region,
                workspace=body.workspace,
                api_key=body.api_key.get_secret_value() if body.api_key else None,
                credential_id=body.credential_id,
            )
            return _project(session, config)
    except (ValueError, service.CredentialUnavailable):
        raise HTTPException(
            status_code=422, detail="Invalid configuration or unavailable credential"
        ) from None


@router.get("/credentials")
def get_credentials(session: Session = db_dependency):
    rows = session.scalars(
        select(ProviderCredential).order_by(ProviderCredential.provider, ProviderCredential.version)
    ).all()
    result = []
    for row in rows:
        try:
            available = bool(service._key(row))
        except service.CredentialUnavailable:
            available = False
        result.append(
            {
                "credential_id": str(row.credential_id),
                "provider": row.provider,
                "version": row.version,
                "enabled": row.enabled,
                "available": available,
            }
        )
    return result


@router.post("/credentials/{credential_id}/disable")
def disable_credential(credential_id: UUID, session: Session = db_dependency):
    try:
        with session.begin():
            service.disable(session, credential_id)
    except service.CredentialUnavailable:
        raise HTTPException(status_code=404, detail="Credential unavailable") from None
    return {"disabled": True}
