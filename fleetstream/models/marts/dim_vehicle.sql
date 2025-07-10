select distinct vehicle_id
from {{ ref('stg_vehicle_telemetry') }}