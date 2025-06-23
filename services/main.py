import asyncio, random, uuid
from datetime import datetime, timezone
from fastapi import FastAPI, BackgroundTasks
from kafka import KafkaProducer
import json

app = FastAPI()
producer = KafkaProducer(
    bootstrap_servers='localhost:9092',
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

# === health check ===
@app.get("/")
def health_check():
    return ({"meesage": "Server is working!"})

# === Get telemery ===
def gen_telemetry():
    return {
        "vehicle_id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "location": {"lat": 52.2 + random.random()/10, "lon": 21.0 + random.random()/10},
        "speed_kmh": round(random.uniform(0,120),1),
        "engine_rpm": random.randint(700,4000),
        "fuel_level_pct": round(random.uniform(10,100),1),
        "fault_codes": [] if random.random()>0.95 else [random.choice(["P0420","P0171"])]
    }

async def stream_telemetry(rate_s: float):
    while True:
        data = gen_telemetry()
        producer.send('vehicle.telemetry.raw', data)
        await asyncio.sleep(rate_s)

@app.post("/start-sim")
async def start_sim(rate: float = 1.0, bg: BackgroundTasks = None):
    """Uruchom symulację co `rate` sekund."""
    bg.add_task(stream_telemetry, rate)
    return {"status": "started", "rate_s": rate}

@app.post("/stop-sim")
async def stop_sim():
    producer.flush()
    return {"status": "stopped"}
