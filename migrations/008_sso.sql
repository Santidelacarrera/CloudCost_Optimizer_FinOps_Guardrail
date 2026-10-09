-- 008_sso.sql — inicio de sesión único (OIDC). Las cuentas SSO se identifican por (emisor, sub) y no por correo: en algunos IdP
-- (Entra) el correo es un atributo editable y enlazar por correo permitiría quedarse con la cuenta de otra persona.
-- Las cuentas SSO no tienen contraseña local: password_hash = '!sso' (no es un hash válido, así que ninguna contraseña lo satisface).

alter table accounts
    add column sso_issuer  text,
    add column sso_subject text,
    add constraint accounts_sso_pair check ((sso_issuer is null) = (sso_subject is null));

create unique index accounts_sso_uq on accounts (sso_issuer, sso_subject) where sso_subject is not null;
