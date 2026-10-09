-- 009_passkeys.sql — WebAuthn / passkeys. Una llave de acceso es un segundo factor (tras la contraseña) o, con verificación del usuario,
-- un inicio de sesión sin contraseña. En la base solo vive la clave PÚBLICA (SubjectPublicKeyInfo DER), nunca un secreto.

create table webauthn_credentials (
    id               uuid primary key default gen_random_uuid(),
    account_id       uuid not null references accounts(id) on delete cascade,
    organization_id  uuid not null references organizations(id),
    credential_id    bytea not null check (length(credential_id) between 1 and 1023),
    public_key       bytea not null,                       -- SubjectPublicKeyInfo DER
    alg              integer not null check (alg in (-7, -8, -257)),
    sign_count       bigint not null default 0,
    aaguid           bytea,
    transports       text[] not null default '{}',
    backup_eligible  boolean not null default false,
    backup_state     boolean not null default false,
    name             text not null check (length(name) between 1 and 60),
    created_at       timestamptz not null default now(),
    last_used_at     timestamptz
);
create unique index webauthn_credential_id_uq on webauthn_credentials (credential_id);
create index webauthn_credentials_account_idx on webauthn_credentials (account_id);

-- Retos de un solo uso (consumidos al verificar). Se busca por el hash del reto que devuelve el navegador.
create table webauthn_challenges (
    token_hash   text primary key,
    purpose      text not null check (purpose in ('register','mfa','login')),
    account_id   uuid references accounts(id) on delete cascade,
    created_at   timestamptz not null default now(),
    expires_at   timestamptz not null
);
create index webauthn_challenges_exp_idx on webauthn_challenges (expires_at);

alter table webauthn_credentials enable row level security;
alter table webauthn_credentials force row level security;
create policy auth_or_tenant on webauthn_credentials using (auth_context() or organization_id = current_org())
    with check (auth_context() or organization_id = current_org());
alter table webauthn_challenges enable row level security;
alter table webauthn_challenges force row level security;
create policy auth_only on webauthn_challenges using (auth_context()) with check (auth_context());

grant select, insert, update, delete on webauthn_credentials, webauthn_challenges to cloudcost_app;

-- Límite por IP en la emisión de retos de inicio de sesión sin contraseña.
alter table auth_login_attempts drop constraint auth_login_attempts_kind_check;
alter table auth_login_attempts add constraint auth_login_attempts_kind_check
    check (kind in ('login','mfa','signup','forgot','resend','passkey'));
