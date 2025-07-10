{{ config(materialized='incremental',
          unique_key='vehicle_id||date_trunc("day", event_dt)') }}

with base as (
    select
        vehicle_id,
        date_trunc('day', event_ts)                            as event_dt,
        avg(speed_kmh)                                         as avg_speed_kmh,
        max(speed_kmh)                                         as max_speed_kmh,
        avg(engine_rpm)                                        as avg_rpm,
        avg(fuel_level_pct)                                    as avg_fuel_pct,
        count_if(array_length(fault_codes) > 0)                as fault_hits
    from {{ ref('stg_vehicle_telemetry') }}
    group by 1, 2
)

select * from base
