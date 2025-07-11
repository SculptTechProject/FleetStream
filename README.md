# **FleetStream 🚀**

**FleetStream** is an experimental sensor‑to‑dashboard playground. It emulates a fleet of vehicles, captures raw telemetry, crunches it in real‑time with **Apache Spark Structured Streaming**, and exposes the results for further analysis.

---

## **✨ What does it do?**

|  **Layer**          |  **Tooling**                                  |  **Role**                                                                                                                  |
| ---------------------- | ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
|  **Simulation**     |  **FastAPI**(containerised REST service)      |  Fires random telemetry events into Kafka (**vehicle.telemetry.raw**)                                                       |
|  **Queue**          |  **Apache Kafka 3.5**                        |  Buffers raw messages and receives aggregated streams                                                                       |
|  **Processing**     |  **Apache Spark 3.5**(Structured Streaming)  |  Reads from**vehicle.telemetry.raw**, computes stats (avg. speed, fuel level, etc.) and writes to**vehicle.telemetry.agg**  |
|  **Orchestration**  |  **Apache Airflow 2.8**                      |  Scheduled batches & housekeeping (e.g. Kafka log compaction, backups)                                                      |
|  **Analytics**      |  **Metabase 0.47**                           |  Live dashboards on the aggregated data                                                                                     |

Everything is wrapped in **Docker Compose** – start / stop the whole stack with a single command.

---

## **🏗️ Architecture at a glance**

```
┌────────────┐   HTTP/JSON   ┌────────────┐        ┌────────┐
│  Simulator ├──────────────►│   Kafka    │◄──────►│ Spark  │
└────────────┘  (vehicle.*)  └────────────┘  topic │(stream)│
                        ▲                    ▲     └────────┘
                        │                    │         │
                        │        DAGs        │         ▼
                 ┌────────────┐   Airflow    │      Metabase
                 │  REST API  │◄─────────────┘     Dashboards
                 └────────────┘
```

---

## **⚡ Quick start**

> **Prerequisites:** Docker & Docker Compose v2 (Linux, macOS or WSL 2).

```
# 1. Clone the repository
$ git clone https://github.com/<your‑handle>/fleetstream.git
$ cd fleetstream/docker

# 2. Build and launch the entire stack (add -d to detach and mute logs)
$ docker compose up --build

# 3. (optional) Create topics if Kafka is fresh
$ bin/create_topics.sh        # helper script

# 4. Open the portals
- Spark UI:  http://localhost:8080
- Metabase:  http://localhost:3000
- Airflow:   http://localhost:8081  (login: admin / admin)

# 5. Start the simulator
$ ../scripts/simulation_start.sh   # ~1 msg/s by default

# 6. Peek at the results. Peek at the results
$ docker exec -it docker-kafka-1 \
  /opt/bitnami/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server kafka:9092 \
  --topic vehicle.telemetry.agg --from-beginning | jq '.'
```

> To stop and wipe volumes: **docker compose down -v**.

---

## **🗂️ Repository layout**

```
.
├─ docker/                       # Compose files, infra images & helpers
│   ├─ docker-compose.yml
│   ├─ dags/                     # Airflow DAGs (mounted into the scheduler)
│   ├─ logs/                     # Local log volume mounts
│   ├─ spark.Dockerfile          # Custom Spark image
│   └─ …                         # Inne pliki konfiguracyjne
├─ services/                     # Application-level code & images
│   ├─ processing/               # Streaming jobs
│   │   └─ spark/stream_agg.py
│   ├─ consumer.py               # Kafka consumer helper
│   ├─ main.py                   # FastAPI entry-point
│   ├─ Dockerfile                # Builds the API container
│   └─ requirements.txt          # Service-specific deps
├─ scripts/                      # Bash helpers – start/stop the simulator
│   ├─ simulation_start.sh
│   └─ simulation_stop.sh
├─ README.md                     # You’re reading it
├─ requirements.txt              # Local dev / tooling deps
└─ .gitignore
```

---

## **🔧 Configuration tips**

The key knobs live in **docker/docker-compose.yml** – adjust partitions, ports or default simulation rate there first.

* KAFKA\_CFG\_ADVERTISED\_LISTENERS** must be **PLAINTEXT://kafka:9092** inside the stack.**
* **SPARK\_MODE** = **master** / **worker** depending on the container.
* **Tweak executor memory in **services/processing/spark/Dockerfile** (add **--executor-memory**).**

---

## **🚀 First things to try**

Once the containers are up and humming you’ll probably want to *see something* rather than stare at logs.

1. **Metabase first‑run wizard** (soon)
   * Open [http://localhost:3000](http://localhost:3000)
   * Create the initial admin user.
   * Add a new *PostgreSQL* database connection **only if** you’ve enabled the future Postgres sink (see Roadmap).
   * Click **Skip** on sample data, then **Ask a question → Native query** and point it to the **vehicle.telemetry.agg** topic via the Kafka JDBC connector (already bundled).
2. **Airflow sanity check** (soon)
   * Visit [http://localhost:8081](http://localhost:8081) (credentials: **admin** / **admin**).
   * Enable the bundled example DAG *fleetstream\_daily\_housekeeping* – it just prints the size of each Kafka topic to the logs every hour.
   * Trigger it manually once and watch the task logs populate.
3. **Build your first dashboard**
   * In Metabase, create a new *Question* with **avg(speed\_kmh)** grouped by *5‑minute bins* and **vehicle\_id**.
   * Save it to a dashboard named ​*Fleet overview*​.
4. **Verify Spark is streaming**
   * Spark UI → **Streaming** tab → confirm the *Input Rate* isn’t flat‑lining.
   * Click on the latest batch to inspect operator metrics.

Feel free to crank the event rate with **curl -X POST http://localhost:8000/start-sim -d 'rate\_hz=10'** (10 msgs/s) – Spark will automatically scale partitions.

---

## **🛣️ Roadmap**

|  **Phase**           |  **Milestone**                                                                                   |  **Why it matters**                              |
| ----------------------- | --------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
|  **🔜 Short‑term**  |  Persist aggregates to**PostgreSQL**and surface them in Metabase via a CDC pipeline (Debezium).  |  Durable storage & SQL joins with reference data  |
|                       |  Bundle**Grafana + Loki**for centralised dashboards and log aggregation.                         |  One place for infra + app metrics                |
|                       |  **GitHub Actions**CI/CD: build & push Docker images, run smoke tests.                           |  Reproducible builds & early breakage detection   |
|  **🛫 Mid‑term**    |  Ship a**Helm chart**so the stack can be deployed on any Kubernetes cluster.                     |  Cloud‑deployable in a single**helm install**    |
|                       |  Add**Prometheus exporters**for Kafka & Spark to enable alerting.                                |  Production‑grade observability                  |
|                       |  Beef‑up the simulator – realistic fault codes, GPS drifts, harsh braking.                      |  More interesting analytics scenarios             |
|  **🌅 Long‑term**   |  **REST gateway for****real OBD‑II / CAN‑bus hardware**ingestion.                              |  Bridge from lab to the road                      |
|                       |  Showcase**stateful & windowed joins**(e.g. geofencing alerts) in Spark.                         |  Advanced stream‑processing patterns             |
|                       |  Explore**Edge deployment**: mini‑Kafka + Spark Connect on Raspberry Pi.                        |  Low‑latency local analytics                     |

*Excited to hack on any of these?* Open an issue or send a PR – contributions welcome! 👋

---

## **📝 License**

Released under the MIT License.

Have fun & drive safe – even if it’s only bytes on the road 🚗💨

