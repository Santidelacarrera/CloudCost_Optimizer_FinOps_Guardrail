-- 004_auth.sql — cuentas propias: contraseña (scrypt + pepper), MFA TOTP, sesiones opacas revocables, tokens de un solo uso y
-- registro de intentos de acceso. Las tablas se leen desde el módulo de autenticación (contexto app.auth = 'on') o, dentro de un tenant,
-- solo las filas de su propia organización; los códigos de recuperación y los intentos de acceso solo son visibles para el módulo de autenticación.

create table accounts (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organizations(id),
    email               text not null,
    full_name           text not null check (length(full_name) between 1 and 120),
    role                text not null check (role in ('ADMIN','FINOPS','SRE','DEVELOPER','AUDITOR','VIEWER')),
    password_hash       text not null,
    email_verified_at   timestamptz,
    mfa_secret_enc      text,                       -- AES-256-GCM; NULL si no hay MFA configurado
    mfa_enabled_at      timestamptz,
    mfa_last_step       bigint,                     -- último intervalo TOTP aceptado (anti-reutilización)
    password_changed_at timestamptz not null default now(),
    last_login_at       timestamptz,
    disabled_at         timestamptz,
    created_at          timestamptz not null default now(),
    constraint accounts_email_lower check (email = lower(email)),
    constraint accounts_email_len   check (length(email) between 3 and 254)
);
create unique index accounts_email_uq on accounts (email);
create index accounts_org_idx on accounts (organization_id);

create table auth_sessions (
    id               uuid primary key default gen_random_uuid(),
    account_id       uuid not null references accounts(id) on delete cascade,
    organization_id  uuid not null references organizations(id),
    token_hash       text not null unique,          -- sha256 del token opaco; el token en claro nunca se guarda
    kind             text not null default 'active' check (kind in ('active','mfa_pending')),
    ip               text,
    user_agent       text,
    created_at       timestamptz not null default now(),
    last_seen_at     timestamptz not null default now(),
    expires_at       timestamptz not null,          -- caducidad absoluta
    revoked_at       timestamptz,
    revoked_reason   text
);
create index auth_sessions_account_idx on auth_sessions (account_id) where revoked_at is null;

create table auth_tokens (
    id               uuid primary key default gen_random_uuid(),
    purpose          text not null check (purpose in ('verify_email','reset_password','invite')),
    token_hash       text not null unique,
    account_id       uuid references accounts(id) on delete cascade,
    organization_id  uuid references organizations(id),
    email            text,                          -- destinatario de una invitación
    role             text check (role in ('ADMIN','FINOPS','SRE','DEVELOPER','AUDITOR','VIEWER')),
    created_by       uuid references accounts(id),
    created_at       timestamptz not null default now(),
    expires_at       timestamptz not null,
    used_at          timestamptz
);
create index auth_tokens_account_idx on auth_tokens (account_id, purpose) where used_at is null;

create table auth_recovery_codes (
    id          uuid primary key default gen_random_uuid(),
    account_id  uuid not null references accounts(id) on delete cascade,
    code_hash   text not null,
    used_at     timestamptz,
    created_at  timestamptz not null default now(),
    unique (account_id, code_hash)
);

-- Intentos: base del bloqueo progresivo y de los límites por IP. Se guarda un hash del correo, no el correo.
create table auth_login_attempts (
    id          bigint generated always as identity primary key,
    kind        text not null check (kind in ('login','mfa','signup','forgot','resend')),
    email_hash  text,
    ip          text,
    success     boolean not null default false,
    created_at  timestamptz not null default now()
);
create index auth_attempts_email_idx on auth_login_attempts (kind, email_hash, created_at desc);
create index auth_attempts_ip_idx    on auth_login_attempts (kind, ip, created_at desc);

-- ---------------------------------------------------------------- RLS
-- Dos formas de acceso: el módulo de autenticación (app.auth = 'on', necesario porque el login ocurre antes de conocer el tenant)
-- o el propio tenant. Sin ninguna de las dos no se ve nada.
create or replace function auth_context() returns boolean
language sql stable as $$ select coalesce(current_setting('app.auth', true), '') = 'on' $$;

do $$
declare t text;
begin
    foreach t in array array['accounts','auth_sessions','auth_tokens'] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format('create policy auth_or_tenant on %I using (auth_context() or organization_id = current_org()) with check (auth_context() or organization_id = current_org())', t);
    end loop;
    foreach t in array array['auth_recovery_codes','auth_login_attempts'] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format('create policy auth_only on %I using (auth_context()) with check (auth_context())', t);
    end loop;
end $$;

-- ---------------------------------------------------------------- Permisos mínimos
-- Registrarse crea la organización (el WITH CHECK de su política exige que id = tenant actual).
grant insert on organizations to cloudcost_app;
grant select, insert, update on accounts to cloudcost_app;            -- las cuentas no se borran: se deshabilitan
grant select, insert, update, delete on auth_sessions, auth_tokens, auth_recovery_codes to cloudcost_app;
grant select, insert, delete on auth_login_attempts to cloudcost_app;
grant usage, select on all sequences in schema public to cloudcost_app;
grant execute on function auth_context() to cloudcost_app;
