-- Datos de desarrollo (SOLO local). Idempotente. Se aplica con SEED_DEV=true scripts/migrate.sh
insert into organizations (id, name, slug)
values ('11111111-1111-1111-1111-111111111111', 'Acme Dev', 'acme-dev')
on conflict do nothing;

-- Se ejecuta como superusuario/propietario; fijamos el tenant solo para que la política WITH CHECK se cumpla.
select set_config('app.current_org', '11111111-1111-1111-1111-111111111111', false) \g /dev/null

insert into cloud_accounts (id, organization_id, provider, account_ref, display_name, regions)
values ('22222222-2222-2222-2222-222222222222', '11111111-1111-1111-1111-111111111111',
        'demo', '000000000000', 'Cuenta demo (datos sintéticos)', '{us-east-1}')
on conflict do nothing;

insert into repositories (id, organization_id, provider, full_name, default_branch, iac_paths)
values ('33333333-3333-3333-3333-333333333333', '11111111-1111-1111-1111-111111111111',
        'local', 'acme/infra-demo', 'main', '{.}')
on conflict do nothing;

select set_config('app.current_org', '', false) \g /dev/null
