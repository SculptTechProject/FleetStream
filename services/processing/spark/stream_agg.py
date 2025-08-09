from pyspark.sql import SparkSession
from pyspark.sql.functions import (
    from_json, col, window, avg,
    to_timestamp
)
from pyspark.sql.types import (
    StructType, StructField,
    StringType, DoubleType, TimestampType,
    ArrayType
)
import os

spark = SparkSession.builder \
      .appName("TelemetryStreaming") \
      .getOrCreate()

# Paths for streaming file sink (make sure these dirs are writable / mounted)
SINK_PATH = os.getenv("SPARK_SINK_PATH", "/tmp/sink/telemetry")
CHK_PATH  = os.getenv("SPARK_CHK_PATH", "/tmp/chk/telemetry")

schema = StructType([
      StructField("vehicle_id", StringType()),
      StructField("timestamp", StringType()),  # ISO 8601 with tz
      StructField("location", StructType([
            StructField("lat", DoubleType()),
            StructField("lon", DoubleType())
      ])),
      StructField("speed_kmh", DoubleType()),
      StructField("engine_rpm", DoubleType()),
      StructField("gear", DoubleType()),
      StructField("fuel_level_pct", DoubleType()),
      StructField("fault_codes", ArrayType(StringType()))
])

raw = (spark.readStream
       .format("kafka")
       .option("kafka.bootstrap.servers", os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"))
       .option("subscribe", os.getenv("KAFKA_TOPIC", "vehicle.telemetry.raw"))
       .option("startingOffsets", os.getenv("KAFKA_START_OFFSETS", "earliest"))
       .load()
       .selectExpr("CAST(value AS STRING) AS json")
       .select(from_json(col("json"), schema).alias("data"))
       .select("data.*")
       # parse ISO8601 with microseconds + timezone, e.g. 2025-08-09T14:49:58.576078+00:00
       .withColumn("event_ts", to_timestamp("timestamp", "yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXX"))
       .withWatermark("event_ts", "2 minutes"))

agg = (raw.groupBy(window(col("event_ts"), "1 minute"),
                   col("vehicle_id"))
            .agg(avg("speed_kmh").alias("avg_speed_kmh")))

query = (agg.writeStream
             # for file sinks use append + watermark so closed windows are emitted
             .outputMode("append")
             .format("parquet")
             .option("path", SINK_PATH)
             .option("checkpointLocation", CHK_PATH)
             .trigger(processingTime=os.getenv("SPARK_TRIGGER", "5 seconds"))
             .start())

query.awaitTermination()