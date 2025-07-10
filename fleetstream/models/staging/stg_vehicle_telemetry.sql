{{ config(materialized='incremental',
          unique_key='event_id',  -- patrz niżej
          incremental_strategy='merge') }}

with src as (

    select
        md5(concat(vehicle_id, timestamp))      as event_id,
        vehicle_id,
        to_timestamp(timestamp)                 as event_ts,
        location.lat                            as lat,
        location.lon                            as lon,
        speed_kmh,
        engine_rpm,
        fuel_level_pct,
        fault_codes                             -- array<string>
    from {{ source('kafka', 'vehicle_telemetry_raw') }}

)

select * from src
where event_ts >= '{{ dbt_utils.dateadd("hour", -48, "current_timestamp") }}'  -- pruning dla inkrementali
