"""SOLO desarrollo: emite tokens para la organización semilla. No se monta si AUTH_MODE != dev o ENV=production."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import Settings, get_settings
from ..schemas import DevTokenIn
from ..security import mint_dev_token

router = APIRouter(tags=["dev"])


@router.post("/dev/token")
def dev_token(body: DevTokenIn, settings: Settings = Depends(get_settings)):
    return {"access_token": mint_dev_token(settings, email=body.email, role=body.role), "token_type": "bearer"}
