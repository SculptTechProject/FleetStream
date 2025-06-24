#!/usr/bin/env bash

docker exec -it docker-kafka-1 \
  kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 \
  --topic vehicle.telemetry.raw \
  --from-beginning \
  --max-messages 5