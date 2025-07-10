import asyncio, random, uuid, json, os
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI
from kafka import KafkaProducer

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC     = os.getenv("KAFKA_TOPIC", "vehicle.telemetry.raw")
sim_task: asyncio.Task | None = None


def gen_telemetry() -> dict:
    return {
        "vehicle_id": str(uuid.uuid4())[:8],
        "timestamp" : datetime.now(timezone.utc).isoformat(),
        "location"  : {"lat": 52.2 + random.random()/10,
                       "lon": 21.0 + random.random()/10},
        "speed_kmh" : round(random.uniform(0, 120), 1),
        "engine_rpm": random.randint(700, 4000),
        "fuel_level_pct": round(random.uniform(10, 100), 1),
        "fault_codes": [] if random.random() > 0.99
                         else [random.choice(["P0420", "P0171"])]
    }

@asynccontextmanager
async def lifespan(app: FastAPI):
    producer = KafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode()
    )
    app.state.producer = producer
    try:
        yield
    finally:
        producer.flush()
        producer.close()

app = FastAPI(lifespan=lifespan)

async def stream(rate_s: float, producer: KafkaProducer):
    while True:
        data = gen_telemetry()
        producer.send(TOPIC, key=data["vehicle_id"].encode(), value=data)
        await asyncio.sleep(rate_s)


@app.post("/start-sim")
async def start_sim(rate: float = 1.0):
    global sim_task
    if sim_task and not sim_task.done():
        return {"status": "already running"}
    sim_task = asyncio.create_task(
        stream(rate, app.state.producer)
    )
    return {"status": "started", "rate_s": rate}


@app.post("/stop-sim")
async def stop_sim():
    global sim_task
    if sim_task:
        sim_task.cancel()
        try:
            await sim_task
        except asyncio.CancelledError:
            pass
        sim_task = None
    return {"status": "stopped"}


@app.get("/")
def health_check():
    return ({"meesage": "Server is working!"})
