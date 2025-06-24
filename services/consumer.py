from kafka import KafkaConsumer
import json

consumer = KafkaConsumer(
    'vehicle.telemetry.raw',
    bootstrap_servers='localhost:9092',
    value_deserializer=lambda m: json.loads(m.decode('utf-8')),
    auto_offset_reset='earliest',
    enable_auto_commit=True,
    group_id='my-consumer-group'
)

for msg in consumer:
    data = msg.value
    print(f"{msg.topic} @ {msg.partition}/{msg.offset}: {data}")
