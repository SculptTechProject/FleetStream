"""Fully-featured vehicle telemetry simulator with FastAPI + Kafka.

▸ One deterministic vehicle (TESTCAR01) following a smooth trajectory
▸ Natural evolution of speed / position / RPM / fuel
▸ Start / stop endpoints; configurable message rate

Run with e.g.
    uvicorn services.main:app --reload --port 8000

Websocket.
    ws://127.0.0.1:8000/ws

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
import logging
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Literal, Deque, Dict, Any, Set

from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

# --- Configure ----------------------------------------------------
VEHICLE_ID = os.getenv("VEHICLE_ID", "TESTCAR01")

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC      = os.getenv("KAFKA_TOPIC", "vehicle.telemetry.raw")
# Driving behaviour / limits
SPORT_MODE = os.getenv("SIM_SPORT_MODE", "0").lower() in ("1", "true", "yes")
SPEED_TOLERANCE_KMH = int(os.getenv("SPEED_TOLERANCE_KMH", "5"))
REDLINE_RPM = int(os.getenv("REDLINE_RPM", "6200"))
UPSHIFT_RPM_SPORT = int(os.getenv("UPSHIFT_RPM_SPORT", "3800"))
# --------------------------------------------------------------------

# Logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("sim")

# Typical speed limits for different road types (km/h)
SPEED_LIMITS = {"urban": 50, "rural": 90, "highway": 130}

RoadType = Literal["urban", "rural", "highway"]

# ──────────────────────────  Transmission  ──────────────────────────
# rpm per 1 km/h for each forward gear – simple, but good enough
GEAR_SPEED_RPM = {1: 110, 2: 70, 3: 50, 4: 40, 5: 33, 6: 28}
MAX_GEAR = max(GEAR_SPEED_RPM)
UPSHIFT_RPM = 3100   # change up when above this
DOWNSHIFT_RPM = 1300 # change down when below this

# Hard cap on vehicle top speed (km/h) so the simulator stays realistic
MAX_SPEED = 180

# ────────────────────────────────  Physics  ───────────────────────────────
@dataclass
class CarState:
    vehicle_id: str = VEHICLE_ID
    lat: float = 52.2304  # Warsaw lat
    lon: float = 21.0122 # Warsaw lon
    speed: float = 0.0  # km/h
    heading: float = 90.0  # degrees (0°=N)
    rpm: int = 800
    gear: int = 1  # start in first
    racing_ticks: int = 0  # counts down “sprint” mode
    # simple traffic / road context
    road_type: RoadType = "urban"   # 'urban', 'rural', 'highway'
    section_ticks: int = 0     # how long we stay on current road section
    red_light_ticks: int = 0   # counts down stop at traffic lights
    fuel_pct: float = 95.0  # %
    faults: list[str] = field(default_factory=list)

    def to_event(self) -> dict:
        """Return a JSON-serialisable telemetry object."""
        limit = SPEED_LIMITS[self.road_type]
        return {
            "vehicle_id": self.vehicle_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "location": {"lat": round(self.lat, 6), "lon": round(self.lon, 6)},
            "speed_kmh": round(self.speed, 1),
            "engine_rpm": self.rpm,
            "gear": self.gear,
            "fuel_level_pct": round(self.fuel_pct, 1),
            "fault_codes": self.faults or [],
            # context & safety
            "road_type": self.road_type,
            "speed_limit_kmh": limit,
            "speeding": round(self.speed, 1) > (limit + SPEED_TOLERANCE_KMH),
            "rpm_over_redline": self.rpm > REDLINE_RPM,
            "gear_mode": "sport" if SPORT_MODE else "normal",
            # naive constant-velocity predictions
            "predicted_location_1s": _predict_location(self, 1.0),
            "predicted_location_5s": _predict_location(self, 5.0),
        }


def _select_section_and_lights(state: CarState) -> None:
    """Advance road-section counters and occasionally trigger red lights."""
    if state.section_ticks == 0:
        state.road_type = random.choices(["urban", "rural", "highway"], weights=[0.5, 0.3, 0.2])[0]
        state.section_ticks = random.randint(20, 90)
    state.section_ticks -= 1

    if state.road_type == "urban" and state.red_light_ticks == 0 and random.random() < 0.05:
        state.red_light_ticks = random.randint(5, 20)

def _compute_target_speed(state: CarState) -> float:
    if state.red_light_ticks > 0:
        state.red_light_ticks -= 1
        return 0.0
    base_limit = SPEED_LIMITS[state.road_type]
    return max(0.0, min(MAX_SPEED, base_limit + random.uniform(-15, 5)))

def _update_heading(state: CarState) -> None:
    state.heading = (state.heading + random.uniform(-3, 3)) % 360

def _predict_location(state: CarState, horizon_s: float) -> dict:
    """Constant-velocity short-term position prediction (no state mutation)."""
    speed_ms = state.speed / 3.6
    R = 6_371_000
    dist = speed_ms * horizon_s
    phi1 = math.radians(state.lat)
    lam1 = math.radians(state.lon)
    theta = math.radians(state.heading)
    delta = dist / R

    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )

    return {"lat": round(math.degrees(phi2), 6), "lon": round(math.degrees(lam2), 6)}

def _move_and_return_dist(state: CarState, speed_ms: float, dt: float) -> float:
    R = 6_371_000  # Earth radius (m)
    dist = speed_ms * dt
    phi1 = math.radians(state.lat)
    lam1 = math.radians(state.lon)
    theta = math.radians(state.heading)
    delta = dist / R

    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )

    state.lat = math.degrees(phi2)
    state.lon = math.degrees(lam2)
    return dist

def _update_transmission(state: CarState) -> None:
    if state.speed == 0:
        state.rpm = 800  # idle
    else:
        coef = GEAR_SPEED_RPM[state.gear]
        state.rpm = max(800, int(state.speed * coef))

    up_rpm = UPSHIFT_RPM_SPORT if SPORT_MODE else UPSHIFT_RPM
    if state.rpm > up_rpm and state.gear < MAX_GEAR:
        state.gear += 1
        state.rpm = max(800, int(state.speed * GEAR_SPEED_RPM[state.gear]))
    elif state.rpm < DOWNSHIFT_RPM and state.gear > 1:
        state.gear -= 1
        state.rpm = max(800, int(state.speed * GEAR_SPEED_RPM[state.gear]))

def _consume_fuel(state: CarState, dist: float) -> None:
    l_per_100km = 5 + 0.04 * (state.speed - 60) ** 2 / 60
    consumed_l = l_per_100km / 100 * (dist / 1000)
    tank_l = 50
    state.fuel_pct = max(state.fuel_pct - consumed_l / tank_l * 100, 0)

def _maybe_emit_fault(state: CarState, dist: float) -> None:
    if random.random() < dist / 500_000:
        state.faults = [random.choice(["P0420", "P0171"])]
    else:
        state.faults.clear()

def advance(state: CarState, dt: float = 1.0) -> None:
    """Advance *state* by *dt* seconds with simple kinematics."""
    _select_section_and_lights(state)

    target_speed = _compute_target_speed(state)
    delta_v = target_speed - state.speed
    base_accel = max(min(delta_v * 0.5, 4.0), -4.5)
    accel = (base_accel * (1.2 if SPORT_MODE else 1.0)) + random.uniform(-1.0, 1.0)

    if state.racing_ticks > 0:
        accel += random.uniform(1.0, 2.0)
        state.racing_ticks -= 1
    elif state.racing_ticks == 0 and random.random() < 0.001:
        state.racing_ticks = random.randint(5, 15)

    speed_ms = max(state.speed / 3.6 + accel * dt, 0.0)
    state.speed = min(speed_ms * 3.6, MAX_SPEED)
    speed_ms = state.speed / 3.6

    _update_heading(state)
    dist = _move_and_return_dist(state, speed_ms, dt)
    _update_transmission(state)
    _consume_fuel(state, dist)
    _maybe_emit_fault(state, dist)


# ──────────────────────────────  Event Store & Consumer  ───────────────────────────
class EventStore:
    def __init__(self, maxlen: int = 2000):
        self.buffer: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self.last_by_vehicle: Dict[str, Dict[str, Any]] = {}
        self.subscribers: Set[asyncio.Queue] = set()

    def add(self, event: Dict[str, Any]) -> None:
        self.buffer.append(event)
        vid = event.get("vehicle_id")
        if vid:
            self.last_by_vehicle[vid] = event
        # broadcast to subscribers (best-effort)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def recent(self, limit: int = 200) -> list[Dict[str, Any]]:
        if limit <= 0:
            return []
        return list(self.buffer)[-limit:]

    def latest_all(self) -> list[Dict[str, Any]]:
        return list(self.last_by_vehicle.values())

    def violations(self, limit: int = 200) -> list[Dict[str, Any]]:
        # look back more to find enough violations, then trim
        candidates = self.recent(limit * 3)
        picked = [e for e in candidates if e.get("speeding") or e.get("rpm_over_redline")]
        return picked[:limit]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


async def consume_loop(consumer: AIOKafkaConsumer, store: EventStore) -> None:
    try:
        async for msg in consumer:
            try:
                event = msg.value  # already deserialized
                if isinstance(event, dict):
                    store.add(event)
            except Exception as e:  # defensive: malformed payload
                logger.warning("Skipping malformed event: %s", e)
    except asyncio.CancelledError:
        pass


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

    # Dashboard store & consumer (for external dashboards and WS broadcast)
    store = EventStore(maxlen=int(os.getenv("STORE_MAXLEN", "2000")))
    consumer = AIOKafkaConsumer(
        TOPIC,
        bootstrap_servers=BOOTSTRAP,
        group_id=os.getenv("KAFKA_DASHBOARD_GROUP", "dashboard"),
        value_deserializer=lambda v: json.loads(v.decode()),
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    await consumer.start()
    consumer_task = asyncio.create_task(consume_loop(consumer, store))
    app.state.store = store        # type: ignore[attr-defined]
    app.state.consumer = consumer  # type: ignore[attr-defined]
    app.state.consumer_task = consumer_task  # type: ignore[attr-defined]
    try:
        yield
    finally:
        consumer_task.cancel()
        with suppress(asyncio.CancelledError):
            await consumer_task
        await consumer.stop()
        await sim.stop()
        await producer.stop()


app = FastAPI(title="Vehicle Simulator", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Dashboard UI endpoint ---------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset=utf-8 />
  <meta name=viewport content="width=device-width,initial-scale=1" />
  <title>FleetStream – Live</title>
  <link rel="preconnect" href="https://cdn.jsdelivr.net" />
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    :root { --bg:#0f1222; --card:#171a2f; --text:#e7e9f3; --muted:#9aa0b4; --ok:#2ecc71; --warn:#f1c40f; --bad:#e74c3c; }
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,Segoe UI,Roboto}
    header{padding:14px 18px;border-bottom:1px solid #22263f;display:flex;gap:14px;align-items:center}
    header .dot{width:10px;height:10px;border-radius:50%;background:#888}
    header .dot.ok{background:var(--ok)} header .dot.bad{background:var(--bad)}
    .grid{display:grid;grid-template-columns: 1.2fr 1fr;gap:16px;padding:16px}
    .card{background:var(--card);border:1px solid #22263f;border-radius:12px;padding:12px;box-shadow:0 6px 20px rgba(0,0,0,.25)}
    .stats{display:grid;grid-template-columns: repeat(6,1fr);gap:10px}
    .stat{background:#121631;border:1px solid #22263f;border-radius:10px;padding:10px;text-align:center}
    .k{font-size:28px;font-weight:700}
    .table{width:100%;border-collapse:collapse}
    .table th,.table td{border-bottom:1px solid #242847;padding:6px 8px;text-align:left;font-size:12px}
    #map{height:360px;border-radius:10px;overflow:hidden}
    .map-wrap{position:relative}
    .ctrl{position:absolute;top:10px;left:10px;z-index:1000;background:#121631cc;color:#e7e9f3;border:1px solid #22263f;border-radius:8px;padding:6px 8px;display:flex;gap:10px;align-items:center}
    .btn{display:inline-block;margin-left:auto;padding:6px 10px;border:1px solid #22263f;border-radius:8px;background:#121631;color:#e7e9f3;text-decoration:none}
    .btn:hover{filter:brightness(1.15)}
    .btn.primary{background:#1e8e3e;border-color:#2a984a}
    .btn.danger{background:#a83232;border-color:#b33}
    .btn:disabled{opacity:.6;cursor:not-allowed}
    .rate{width:70px;background:#0f1222;color:#e7e9f3;border:1px solid #22263f;border-radius:6px;padding:4px 6px}
    @media (max-width: 980px){ .grid{grid-template-columns:1fr} #map{height:300px} .stats{grid-template-columns: repeat(3,1fr)} }
  </style>
</head>
<body>
  <header>
    <div class="dot" id="wsDot"></div>
    <strong>FleetStream Live</strong>
    <span id="statusTxt" style="color:var(--muted)">connecting…</span>
    <label style="margin-left:12px">Hz:
      <input id="rateIn" class="rate" type="number" value="5" min="0.2" max="50" step="0.2">
    </label>
    <button class="btn primary" id="btnStart" type="button" title="POST /start-sim?rate_hz=...">Start</button>
    <button class="btn danger" id="btnStop" type="button" title="POST /stop-sim">Stop</button>
    <a href="/telemetry/export.csv?limit=2000" class="btn" id="btnCsv">Export CSV</a>
  </header>

  <div class="grid">
    <section class="card">
      <div class="map-wrap">
        <div id="map"></div>
        <div class="ctrl">
          <label><input type="checkbox" id="tHeat" checked> Heatmap</label>
          <label><input type="checkbox" id="tTrail" checked> Trail</label>
        </div>
      </div>
    </section>

    <section class="card">
      <canvas id="chart" height="160"></canvas>
      <div class="stats" style="margin-top:10px">
        <div class="stat"><div>Speed</div><div class="k" id="sSpeed">–</div></div>
        <div class="stat"><div>RPM</div><div class="k" id="sRpm">–</div></div>
        <div class="stat"><div>Gear</div><div class="k" id="sGear">–</div></div>
        <div class="stat"><div>Road</div><div class="k" id="sRoad">–</div></div>
        <div class="stat"><div>Limit</div><div class="k" id="sLimit">–</div></div>
        <div class="stat"><div>Fuel</div><div class="k" id="sFuel">–</div></div>
      </div>
    </section>

    <section class="card" style="grid-column:1 / -1">
      <h3 style="margin:6px 0 10px">Violations</h3>
      <table class="table" id="violTable">
        <thead><tr><th>time</th><th>speed</th><th>rpm</th><th>limit</th><th>road</th><th>type</th></tr></thead>
        <tbody></tbody>
      </table>
    </section>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <script>
    const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${wsProto}://${location.host}/ws`);
    const dot = document.getElementById('wsDot');
    const stxt = document.getElementById('statusTxt');
    const rateIn = document.getElementById('rateIn');
    const startBtn = document.getElementById('btnStart');
    const stopBtn = document.getElementById('btnStop');

    async function startSim(){
      const hz = Math.max(0.2, Math.min(50, parseFloat(rateIn.value)||1));
      startBtn.disabled = true;
      try{
        const res = await fetch(`/start-sim?rate_hz=${encodeURIComponent(hz)}`, {method:'POST'});
        const body = await res.json().catch(()=> ({}));
        if (res.ok){
          stxt.textContent = `online (rate ${hz} Hz)`;
        }else{
          stxt.textContent = `start error: ${body.detail||res.status}`;
        }
      }finally{
        startBtn.disabled = false;
      }
    }

    async function stopSim(){
      stopBtn.disabled = true;
      try{
        const res = await fetch('/stop-sim', {method:'POST'});
        const body = await res.json().catch(()=> ({}));
        if (res.ok){
          stxt.textContent = 'stopped';
        }else{
          stxt.textContent = `stop error: ${body.detail||res.status}`;
        }
      }finally{
        stopBtn.disabled = false;
      }
    }

    startBtn.addEventListener('click', startSim);
    stopBtn.addEventListener('click', stopSim);
    ws.onopen = () => { dot.classList.add('ok'); stxt.textContent = 'online'; };
    ws.onclose = () => { dot.classList.remove('ok'); dot.classList.add('bad'); stxt.textContent = 'disconnected'; };

    let map = L.map('map');
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap' }).addTo(map);
    let marker = L.marker([52.23, 21.01]).addTo(map);
    map.setView([52.23, 21.01], 13);

    // Heatmap + ślad
    const TRACK_MAX = 2000;
    const heat = L.heatLayer([], { radius: 22, blur: 18, maxZoom: 18, minOpacity: 0.25,
                                   gradient: {0.0:'#00bcd4',0.5:'#ffc107',0.8:'#ff5722',1.0:'#f44336'} });
    const trailGroup = L.layerGroup().addTo(map);
    let lastPt = null;              // previous point for segment drawing
    const segList = [];             // keep last N segments for pruning
    heat.addTo(map);

    // Przełączniki
    const chkHeat = document.getElementById('tHeat');
    const chkTrail = document.getElementById('tTrail');
    chkHeat.addEventListener('change', ()=>{ if(chkHeat.checked) heat.addTo(map); else map.removeLayer(heat); });
    chkTrail.addEventListener('change', ()=>{ if(chkTrail.checked) trailGroup.addTo(map); else map.removeLayer(trailGroup); });

    // Wykres
    const ctx = document.getElementById('chart');
    const chart = new Chart(ctx, {
      type: 'line',
      data: { labels: [], datasets: [
        { label: 'km/h', data: [], tension: .25 },
        { label: 'rpm',  data: [], tension: .25 }
      ]},
      options: { responsive: true, animation: false, scales: { x: { display: false } } }
    });
    const MAX_POINTS = 300;

    const el = id => document.getElementById(id);
    const vTbody = document.querySelector('#violTable tbody');

    function pushChart(speed, rpm){
      const d = chart.data; d.labels.push(''); d.datasets[0].data.push(speed); d.datasets[1].data.push(rpm);
      while (d.labels.length > MAX_POINTS){ d.labels.shift(); d.datasets.forEach(ds => ds.data.shift()); }
      chart.update();
    }

    function fmt(n){ return typeof n==='number' ? n.toFixed(1) : '–'; }

    function colorForSpeed(s){
      if (s < 20) return '#2ecc71';
      if (s < 40) return '#27ae60';
      if (s < 60) return '#00a8ff';
      if (s < 80) return '#f1c40f';
      if (s < 110) return '#e67e22';
      return '#e74c3c';
    }

    function pushGeo(lat, lon, speed, isViolation){
      if (typeof lat !== 'number' || typeof lon !== 'number') return;
      let w = Math.max(0.2, Math.min(1.0, (speed||0)/130));   // waga heatmapy ~ prędkość
      if (isViolation) w = Math.max(w, 0.9);                  // naruszenie -> mocniej
      heat.addLatLng([lat, lon, w]);
      if (lastPt) {
        const seg = L.polyline([lastPt, [lat, lon]], { color: colorForSpeed(speed||0), weight: 4, opacity: 0.9 });
        seg.addTo(trailGroup);
        segList.push(seg);
        if (segList.length > TRACK_MAX) {
          const old = segList.shift();
          if (old) trailGroup.removeLayer(old);
        }
      }
      lastPt = [lat, lon];
    }

    function updateStats(ev){
      el('sSpeed').textContent = fmt(ev.speed_kmh);
      el('sRpm').textContent   = Math.round(ev.engine_rpm);
      el('sGear').textContent  = ev.gear;
      el('sRoad').textContent  = ev.road_type;
      el('sLimit').textContent = ev.speed_limit_kmh;
      el('sFuel').textContent  = fmt(ev.fuel_level_pct)+'%';
      if (ev.location){
        const {lat, lon} = ev.location;
        marker.setLatLng([lat, lon]);
        map.setView([lat, lon]);
        pushGeo(lat, lon, ev.speed_kmh, (ev.speeding||ev.rpm_over_redline));
      }
      pushChart(ev.speed_kmh, ev.engine_rpm);
    }

    function addViolation(ev){
      const tr = document.createElement('tr');
      const typ = ev.speeding ? 'speeding' : (ev.rpm_over_redline ? 'redline' : '');
      tr.innerHTML = `<td>${ev.timestamp?.replace('T',' ').replace('Z','')}</td>
                      <td>${fmt(ev.speed_kmh)}</td>
                      <td>${Math.round(ev.engine_rpm||0)}</td>
                      <td>${ev.speed_limit_kmh ?? ''}</td>
                      <td>${ev.road_type ?? ''}</td>
                      <td>${typ}</td>`;
      if (typ==='speeding') tr.style.color = 'var(--warn)';
      if (typ==='redline') tr.style.color = 'var(--bad)';
      vTbody.prepend(tr);
      while (vTbody.children.length > 50) vTbody.removeChild(vTbody.lastChild);
    }

    async function loadInitial(){
      try{
        const snap = await (await fetch('/telemetry/latest')).json();
        if (snap.items && snap.items[0]) updateStats(snap.items[0]);

        // seed mapy historią
        const rec = await (await fetch('/telemetry/recent?limit=500')).json();
        (rec.items||[]).forEach(ev => {
          if (ev.location) pushGeo(ev.location.lat, ev.location.lon, ev.speed_kmh, (ev.speeding||ev.rpm_over_redline));
        });

        const viol = await (await fetch('/telemetry/violations?limit=20')).json();
        (viol.items||[]).forEach(addViolation);
      }catch(e){ console.warn('init fail', e); }
    }

    ws.onmessage = (m)=>{
      try{
        const msg = JSON.parse(m.data);
        if (msg.type==='snapshot' && msg.items && msg.items[0]){ updateStats(msg.items[0]); }
        if (msg.type==='event' && msg.data){
          updateStats(msg.data);
          if (msg.data.speeding || msg.data.rpm_over_redline) addViolation(msg.data);
        }
      }catch(e){ /* ignore */ }
    };

    loadInitial();
  </script>
</body>
</html>
"""

@app.post("/start-sim")
async def start_sim(rate_hz: float = 1.0):
    """Start streaming telemetry at *rate_hz* messages per second (0 < rate_hz <= 50)."""
    if rate_hz <= 0 or rate_hz > 50:
        raise HTTPException(status_code=422, detail="rate_hz must be in (0, 50].")
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
    return {"running": sim.running(), "last_event": sim._state.to_event()}

@app.get("/telemetry/latest")
async def telemetry_latest():
    store: EventStore = app.state.store  # type: ignore[attr-defined]
    return {"items": store.latest_all()}

@app.get("/telemetry/recent")
async def telemetry_recent(limit: int = 200):
    store: EventStore = app.state.store  # type: ignore[attr-defined]
    limit = max(1, min(limit, 2000))
    return {"items": store.recent(limit)}

@app.get("/telemetry/violations")
async def telemetry_violations(limit: int = 200):
    store: EventStore = app.state.store  # type: ignore[attr-defined]
    limit = max(1, min(limit, 2000))
    return {"items": store.violations(limit)}

@app.websocket("/ws")
async def ws_events(ws: WebSocket):
    await ws.accept()
    store: EventStore = app.state.store  # type: ignore[attr-defined]
    q = store.subscribe()
    try:
        # initial snapshot
        await ws.send_json({"type": "snapshot", "items": store.latest_all()})
        while True:
            item = await q.get()
            await ws.send_json({"type": "event", "data": item})
    except WebSocketDisconnect:
        pass
    finally:
        store.unsubscribe(q)

# CSV export endpoint
@app.get("/telemetry/export.csv")
async def telemetry_export_csv(limit: int = 2000):
    store: EventStore = app.state.store  # type: ignore[attr-defined]
    limit = max(1, min(limit, 20000))
    rows = store.recent(limit)
    import io, csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "timestamp","vehicle_id","lat","lon","speed_kmh","engine_rpm","gear",
        "fuel_level_pct","road_type","speed_limit_kmh","speeding","rpm_over_redline",
    ])
    for e in rows:
        loc = e.get("location") or {}
        w.writerow([
            e.get("timestamp"),
            e.get("vehicle_id"),
            loc.get("lat"),
            loc.get("lon"),
            e.get("speed_kmh"),
            e.get("engine_rpm"),
            e.get("gear"),
            e.get("fuel_level_pct"),
            e.get("road_type"),
            e.get("speed_limit_kmh"),
            1 if e.get("speeding") else 0,
            1 if e.get("rpm_over_redline") else 0,
        ])
    buf.seek(0)
    filename = f"telemetry_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers=headers)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/")
def root():
    return {"message": "Vehicle simulator ready"}
