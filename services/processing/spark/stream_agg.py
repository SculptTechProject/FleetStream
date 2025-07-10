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

spark = SparkSession.builder \
      .appName("TelemetryStreaming") \
      .getOrCreate()

schema = StructType([
      StructField("vehicle_id", StringType()),
      StructField("timestamp", StringType()),
      StructField("location", StructType([
            StructField("lat", DoubleType()),
            StructField("lon", DoubleType())
      ])),
      StructField("speed_kmh", DoubleType()),
      StructField("engine_rpm", DoubleType()),
      StructField("fuel_level_pct", DoubleType()),
      StructField("fault_codes", ArrayType(StringType()))
])

raw = (spark.readStream
       .format("kafka")
       .option("kafka.bootstrap.servers", "kafka:9092")
       .option("subscribe", "vehicle.telemetry.raw")
       .option("startingOffsets", "earliest")
       .load()
       .selectExpr("CAST(value AS STRING) AS json")
       .select(from_json(col("json"), schema).alias("data"))
       .select("data.*")
       .withColumn("timestamp",
                   to_timestamp("timestamp"))
       .withWatermark("timestamp", "2 minutes"))

agg = (raw.groupBy(window(col("timestamp"), "1 minute"),
                   col("vehicle_id"))
            .agg(avg("speed_kmh").alias("avg_speed_kmh")))

query = (agg.writeStream
             .outputMode("complete")
             .format("parquet")
             .option("truncate", "false")
             .start())

query.awaitTermination()