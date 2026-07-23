-- À exécuter dans Supabase → SQL Editor
-- Une seule ligne, état global du bot (pause d'urgence).

create table if not exists system_state (
    id int primary key default 1,
    paused boolean default false,
    paused_by text,
    paused_reason text,
    updated_at timestamptz default now()
);

insert into system_state (id, paused) values (1, false)
on conflict (id) do nothing;

drop trigger if exists trg_system_state_updated_at on system_state;
create trigger trg_system_state_updated_at
    before update on system_state
    for each row execute function update_updated_at();
