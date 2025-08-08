#!/usr/bin/env bash

docker compose exec kafka \
  kafka-console-consumer.sh \
    --bootstrap-server kafka:9092 \
    --topic vehicle.telemetry.raw \
    --from-beginning \
    --property print.key=true \
    --property key.separator=" | " \
    --max-messages 150