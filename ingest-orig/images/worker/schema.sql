/*
 * Primary data tables from radio-locator scrape
 */

drop schema if exists data cascade;
create schema data;

drop table if exists data.station cascade;
create table data.station
(
    station_id integer not null primary key,

    state text not null,
    callsign char(4) not null,
    band char(2) not null check(band in ('FM', 'AM', 'FL')),
    freq real not null,

    stream_url text,

    city text,
    school text,
    format text,
    website text,
    owner text,
    address text,
    phone text,
    fax text,
    status text,
    power text,
    height text,
    antenna_pattern text,
    license_granted text,
    license_expires text,
    last_fcc_update text,

    unique(callsign, band)
);

create index station_names
on data.station
    ((callsign || '-' || band));

/*
 * Control tables and views for the workers
 */

drop schema if exists app cascade;
create schema app;

-- load station_id values here, currently by hand, to start ingesting them
drop table if exists app.jobs cascade;
create table app.jobs
(
  station_id integer not null primary key
             references data.station
             on delete restrict,

  create_dt timestamptz not null default now(),
  error_count integer not null default 0,
  last_error text
);

drop table if exists app.chunks cascade;
create table app.chunks
(
    chunk_id bigserial not null primary key,

    station_id integer not null
               references data.station
               on delete restrict,

    create_dt timestamptz not null default now(),
    s3_url text not null
);

-- Overall job status report
create or replace view app.stats as
select
    count(*) as cnt,
    count(l.station_id) as count_working,
    sum((j.error_count > 0)::int) as count_failed,
    max(j.error_count) as highest_error_count,
    min(j.create_dt) as oldest_create_dt
from app.jobs j
    left join
    (
        select
            objid as station_id
        from pg_locks pli
        where
            pli.locktype = 'advisory' and
            pli.classid = 0 and
            pli."mode" = 'ExclusiveLock'
    ) l using (station_id);

-- Streams that are currently running correctly
create or replace view app.running as
select
    j.station_id
from app.jobs j
    inner join
    (
        select
            pli.objid as station_id
        from pg_locks pli
        where
            pli.locktype = 'advisory' and
            pli.classid = 0 and
            pli."mode" = 'ExclusiveLock'
    ) pl using(station_id);

-- Streams in the queue that aren't currently running
create or replace view app.waiting as
select
    j.station_id
from app.jobs j
    left join
    (
        select
            pli.objid as station_id
        from pg_locks pli
        where
            pli.locktype = 'advisory' and
            pli.classid = 0 and
            pli."mode" = 'ExclusiveLock'
    ) pl using(station_id)
where
    pl.station_id is null;

-- Streams that have ever failed and whether they're currently running
create or replace view app.failed as
select
    j.station_id,
    (pl.station_id is not null) as running
from app.jobs j
    left join
    (
        select
            pli.objid as station_id
        from pg_locks pli
        where
            pli.locktype = 'advisory' and
            pli.classid = 0 and
            pli."mode" = 'ExclusiveLock'
    ) pl using(station_id)
where
    j.error_count > 0;

