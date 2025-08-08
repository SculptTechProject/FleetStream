from kafka import KafkaConsumer
import psycopg2, json

conn = psycopg2.connect(
    host="postgres", dbname="fleet",
    user="metriq", password="metriq"
)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS telemetry_raw (
    id SERIAL PRIMARY KEY,
    vehicle_id TEXT,
    ts TIMESTAMP,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    speed_kmh DOUBLE PRECISION,
    engine_rpm DOUBLE PRECISION,
    fuel_pct DOUBLE PRECISION,
    fault_codes TEXT[]
);
""")
conn.commit()

consumer = KafkaConsumer(
    'vehicle.telemetry.raw',
    bootstrap_servers='kafka:9092',
    value_deserializer=lambda m: json.loads(m.decode()),
    auto_offset_reset='earliest',
    enable_auto_commit=True,
    group_id='fleet-cg'
)

for msg in consumer:
    d = msg.value
    cur.execute(
        """INSERT INTO telemetry_raw
           (vehicle_id, ts, lat, lon, speed_kmh, engine_rpm, fuel_pct, fault_codes)
           VALUES (%s,%s,%s,%s,%s,%s,%s, %s)""",
        (
            d["vehicle_id"],
            d["timestamp"],
            d["location"]["lat"],
            d["location"]["lon"],
            d["speed_kmh"],
            d["engine_rpm"],
            d["fuel_level_pct"],
            d["fault_codes"]
        )
    )
    conn.commit()
