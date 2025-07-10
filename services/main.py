"""Fully-featured vehicle telemetry simulator with FastAPI + Kafka.

▸ One deterministic vehicle (TESTCAR01) following a smooth trajectory
▸ Natural evolution of speed / position / RPM / fuel
▸ Start / stop endpoints; configurable message rate

Run with e.g.
    uvicorn simulator:app --reload --port 8000

Environment variables:
    KAFKA_BOOTSTRAP_SERVERS   (default "localhost:9092")
    KAFKA_TOPIC               (default "vehicle.telemetry.raw")
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, HTTPException

# ────────────────────────────────  Config  ────────────────────────────────
BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC: str = os.getenv("KAFKA_TOPIC", "vehicle.telemetry.raw")
VEHICLE_ID = os.getenv("VEHICLE_ID", "TESTCAR01")

# ────────────────────────────────  Physics  ───────────────────────────────
@dataclass
class CarState:
    vehicle_id: str = VEHICLE_ID
    lat: float = 52.2304  # Warszawa centrum
    lon: float = 21.0122
    speed: float = 0.0  # km/h
    heading: float = 90.0  # degrees (0°=N)
    rpm: int = 800
    fuel_pct: float = 95.0  # %
    faults: list[str] = None

    def to_event(self) -> dict:
        """Return a JSON-serialisable telemetry object."""
        return {
            "vehicle_id": self.vehicle_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "location": {"lat": round(self.lat, 6), "lon": round(self.lon, 6)},
            "speed_kmh": round(self.speed, 1),
            "engine_rpm": self.rpm,
            "fuel_level_pct": round(self.fuel_pct, 1),
            "fault_codes": self.faults or [],
        }


def advance(state: CarState, dt: float = 1.0) -> None:
    """Advance *state* by *dt* seconds with simple kinematics."""
    # Smooth acceleration / deceleration (±2 m/s²)
    accel = random.uniform(-2, 2)  # m/s²
    speed_ms = max(state.speed / 3.6 + accel * dt, 0.0)
    state.speed = speed_ms * 3.6

    # Heading meanders slightly
    state.heading = (state.heading + random.uniform(-3, 3)) % 360

    # Haversine forward calculation
    R = 6_371_000  # Earth radius (m)
    dist = speed_ms * dt  # metres travelled
    phi1 = math.radians(state.lat)
    lam1 = math.radians(state.lon)
    theta = math.radians(state.heading)
    delta = dist / R

    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta)
        + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )

    state.lat = math.degrees(phi2)
    state.lon = math.degrees(lam2)

    # Engine RPM roughly proportional to speed (idle 800 RPM)
    state.rpm = int(800 + state.speed * 45)

    # Fuel consumption: simple quadratic curve with minimum ~5 l/100km @ 60 km/h
    l_per_100km = 5 + 0.04 * (state.speed - 60) ** 2 / 60
    consumed_l = l_per_100km / 100 * (dist / 1000)
    tank_l = 50
    state.fuel_pct = max(state.fuel_pct - consumed_l / tank_l * 100, 0)

    # Rare fault codes (~1 per 500 km)
    if random.random() < dist / 500_000:
        state.faults = [random.choice(["P0420", "P0171"])]
    else:
        state.faults = []


# ──────────────────────────────  Simulator  ───────────────────────────────
class Simulator:
    """Background task producing telemetry at a fixed rate (Hz)."""

    def __init__(self, producer: AIOKafkaProducer):
        self._producer = producer
        self._task: Optional[asyncio.Task] = None
        self._state = CarState()

    async def _loop(self, period_s: float):
        try:
            while True:
                advance(self._state, period_s)
                await self._producer.send_and_wait(TOPIC,
                                                    key=self._state.vehicle_id.encode(),
                                                    value=self._state.to_event(),
                                                    )
                await asyncio.sleep(period_s)
        except asyncio.CancelledError:
            # graceful cancel
            pass

    async def start(self, rate_hz: float = 1.0):
        if self._task and not self._task.done():
            raise RuntimeError("Simulator already running")
        period = 1.0 / rate_hz
        self._task = asyncio.create_task(self._loop(period))

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def running(self) -> bool:
        return self._task is not None and not self._task.done()


# ───────────────────────────────  FastAPI  ────────────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    producer = AIOKafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode(),
    )
    await producer.start()
    sim = Simulator(producer)
    app.state.producer = producer  # type: ignore[attr-defined]
    app.state.simulator = sim      # type: ignore[attr-defined]
    try:
        yield
    finally:
        await sim.stop()
        await producer.stop()


app = FastAPI(title="Vehicle Simulator", lifespan=lifespan)


@app.post("/start-sim")
async def start_sim(rate_hz: float = 1.0):
    """Start streaming telemetry at *rate_hz* messages per second."""
    sim: Simulator = app.state.simulator  # type: ignore[attr-defined]
    if sim.running():
        raise HTTPException(status_code=409, detail="Simulator already running")
    await sim.start(rate_hz)
    return {"status": "started", "rate_hz": rate_hz}


@app.post("/stop-sim")
async def stop_sim():
    sim: Simulator = app.state.simulator  # type: ignore[attr-defined]
    if not sim.running():
        raise HTTPException(status_code=409, detail="Simulator not running")
    await sim.stop()
    return {"status": "stopped"}


@app.get("/status")
async def status():
    sim: Simulator = app.state.simulator  # type: ignore[attr-defined]
    return {"running": sim.running()}


@app.get("/")
def root():
    return {"message": "Vehicle simulator ready"}
