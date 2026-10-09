"""API de cuentas propias. El navegador no llama aquí: lo hace el BFF de Next.js, que guarda el token en una cookie httpOnly."""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response

from .. import metrics
from ..auth import mailer
from ..config import Settings, get_settings
from ..schemas import (
    ChangePasswordIn,
    EmailIn,
    InviteIn,
    LoginIn,
    MemberPatchIn,
    MfaConfirmIn,
    MfaEnableIn,
    MfaSetupIn,
    MfaVerifyIn,
    ResetPasswordIn,
    SignupIn,
    SsoCallbackIn,
    TokenIn,
)
from ..security import Principal, require, require_session
from ..services import accounts, sso

router = APIRouter(prefix="/auth", tags=["auth"])


def _enabled(settings: Settings = Depends(get_settings)) -> Settings:
    if not settings.auth_local_enabled:
        raise HTTPException(404, "Las cuentas propias están desactivadas")
    return settings


def _client(request: Request, settings: Settings) -> tuple[str | None, str | None]:
    """IP y user-agent del cliente. X-Forwarded-For solo se respeta si se declara un proxy propio (AUTH_TRUST_FORWARDED): se usa la
    última entrada, la que añadió nuestro proxy; las anteriores las controla el cliente."""
    cand = request.client.host if request.client else ""
    if settings.auth_trust_forwarded:
        cand = request.headers.get("x-forwarded-for", "").split(",")[-1].strip() or cand
    try:
        ip = str(ipaddress.ip_address(cand))
    except ValueError:
        ip = None
    return ip, request.headers.get("user-agent", "")[:300] or None


def _flush(settings: Settings, outbox: list[mailer.Mail], bg: BackgroundTasks) -> None:
    for mail in outbox:                       # el envío va en segundo plano: el tiempo de respuesta no delata si el correo existe
        bg.add_task(mailer.send, settings, mail)


@router.get("/config")
def config(settings: Settings = Depends(get_settings)):
    return {"local_enabled": settings.auth_local_enabled, "signup_open": settings.auth_signup_open, "dev_login_enabled": settings.auth_mode == "dev" and settings.env != "production",
            "password_min_length": 12, "mfa_issuer": settings.auth_mfa_issuer, **sso.public_info(settings)}


@router.get("/sso/start")
def sso_start(settings: Settings = Depends(get_settings)):
    """Devuelve la URL del IdP y un token de flujo firmado que el BFF guarda en una cookie httpOnly (nunca llega al JavaScript)."""
    return sso.start(settings)


@router.post("/sso/callback")
def sso_callback(body: SsoCallbackIn, request: Request, settings: Settings = Depends(get_settings)):
    ip, ua = _client(request, settings)
    out = sso.callback(settings, code=body.code, state=body.state, flow_token=body.flow_token, ip=ip, ua=ua)
    metrics.AUTH_LOGINS.labels("sso_ok").inc()
    return out


@router.post("/signup", status_code=202)
def signup(body: SignupIn, request: Request, bg: BackgroundTasks, settings: Settings = Depends(_enabled)):
    ip, _ = _client(request, settings)
    outbox: list[mailer.Mail] = []
    out = accounts.signup(settings, email=body.email, password=body.password, full_name=body.full_name,
                          organization_name=body.organization_name, invite_token=body.invite_token, ip=ip, outbox=outbox)
    _flush(settings, outbox, bg)
    return out


@router.post("/verify-email")
def verify_email(body: TokenIn, _: Settings = Depends(_enabled)):
    return accounts.verify_email(body.token)


@router.post("/resend-verification", status_code=202)
def resend_verification(body: EmailIn, request: Request, bg: BackgroundTasks, settings: Settings = Depends(_enabled)):
    ip, _ = _client(request, settings)
    outbox: list[mailer.Mail] = []
    out = accounts.resend_verification(settings, email=body.email, ip=ip, outbox=outbox)
    _flush(settings, outbox, bg)
    return out


@router.post("/login")
def login(body: LoginIn, request: Request, settings: Settings = Depends(_enabled)):
    ip, ua = _client(request, settings)
    out = accounts.login(settings, email=body.email, password=body.password, ip=ip, ua=ua)
    metrics.AUTH_LOGINS.labels(out.get("status", "unknown")).inc()
    return out


@router.post("/mfa/verify")
def mfa_verify(body: MfaVerifyIn, request: Request, bg: BackgroundTasks, settings: Settings = Depends(_enabled)):
    ip, ua = _client(request, settings)
    outbox: list[mailer.Mail] = []
    out = accounts.mfa_verify(settings, pending_token=body.pending_token, code=body.code, ip=ip, ua=ua, outbox=outbox)
    _flush(settings, outbox, bg)
    return out


@router.post("/logout", status_code=204)
def logout(p: Principal = Depends(require_session)):
    accounts.logout(p.session_id)
    return Response(status_code=204)


@router.get("/me")
def me(p: Principal = Depends(require_session)):
    return accounts.me(p)


@router.post("/forgot-password", status_code=202)
def forgot_password(body: EmailIn, request: Request, bg: BackgroundTasks, settings: Settings = Depends(_enabled)):
    ip, _ = _client(request, settings)
    outbox: list[mailer.Mail] = []
    out = accounts.forgot_password(settings, email=body.email, ip=ip, outbox=outbox)
    _flush(settings, outbox, bg)
    return out


@router.post("/reset-password")
def reset_password(body: ResetPasswordIn, bg: BackgroundTasks, settings: Settings = Depends(_enabled)):
    outbox: list[mailer.Mail] = []
    out = accounts.reset_password(settings, token=body.token, password=body.password, outbox=outbox)
    _flush(settings, outbox, bg)
    return out


@router.post("/change-password")
def change_password(body: ChangePasswordIn, bg: BackgroundTasks, p: Principal = Depends(require_session), settings: Settings = Depends(_enabled)):
    outbox: list[mailer.Mail] = []
    accounts.change_password(settings, p, body.current_password, body.new_password, outbox)
    _flush(settings, outbox, bg)
    return {"status": "changed"}


# ---- sesiones
@router.get("/sessions")
def sessions(p: Principal = Depends(require_session)):
    return accounts.list_sessions(p)


@router.post("/sessions/revoke-others")
def revoke_others(p: Principal = Depends(require_session)):
    return {"revoked": accounts.revoke_other_sessions(p)}


@router.delete("/sessions/{session_id}", status_code=204)
def revoke_session(session_id: str, p: Principal = Depends(require_session)):
    accounts.revoke_session(p, session_id)
    return Response(status_code=204)


# ---- 2FA
@router.post("/mfa/setup")
def mfa_setup(body: MfaSetupIn, p: Principal = Depends(require_session), settings: Settings = Depends(_enabled)):
    return accounts.mfa_setup(settings, p, body.password)


@router.post("/mfa/enable")
def mfa_enable(body: MfaEnableIn, bg: BackgroundTasks, p: Principal = Depends(require_session), settings: Settings = Depends(_enabled)):
    outbox: list[mailer.Mail] = []
    out = accounts.mfa_enable(settings, p, body.code, outbox)
    _flush(settings, outbox, bg)
    return out


@router.post("/mfa/disable")
def mfa_disable(body: MfaConfirmIn, bg: BackgroundTasks, p: Principal = Depends(require_session), settings: Settings = Depends(_enabled)):
    outbox: list[mailer.Mail] = []
    accounts.mfa_disable(settings, p, body.password, body.code, outbox)
    _flush(settings, outbox, bg)
    return {"status": "disabled"}


@router.post("/mfa/recovery-codes")
def recovery_codes(body: MfaConfirmIn, p: Principal = Depends(require_session), settings: Settings = Depends(_enabled)):
    return accounts.regenerate_recovery_codes(settings, p, body.password, body.code)


# ---- equipo (ADMIN)
@router.get("/members")
def members(p: Principal = Depends(require("ADMIN"))):
    return accounts.list_members(p.org_id)


@router.patch("/members/{member_id}")
def patch_member(member_id: str, body: MemberPatchIn, p: Principal = Depends(require("ADMIN")), _: Principal = Depends(require_session)):
    return accounts.update_member(p, member_id, role=body.role, disabled=body.disabled)


@router.get("/invitations")
def invitations(p: Principal = Depends(require("ADMIN"))):
    return accounts.list_invitations(p.org_id)


@router.post("/invitations", status_code=201)
def invite(body: InviteIn, bg: BackgroundTasks, p: Principal = Depends(require("ADMIN")), settings: Settings = Depends(_enabled),
           _: Principal = Depends(require_session)):
    outbox: list[mailer.Mail] = []
    out = accounts.invite_member(settings, p, email=body.email, role=body.role, outbox=outbox)
    _flush(settings, outbox, bg)
    return out


@router.delete("/invitations/{invitation_id}", status_code=204)
def revoke_invitation(invitation_id: str, p: Principal = Depends(require("ADMIN"))):
    accounts.revoke_invitation(p.org_id, invitation_id)
    return Response(status_code=204)
