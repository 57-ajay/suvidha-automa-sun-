import asyncio
import os
import time
import urllib.request

PROJECT_ID = os.environ.get("PROJECT_ID", "cabswale-ai")
METRIC_TYPE = "custom.googleapis.com/automation_agent/slot_utilization"


def _metadata(path: str) -> str:
    req = urllib.request.Request(
        f"http://metadata.google.internal/computeMetadata/v1/{path}",
        headers={"Metadata-Flavor": "Google"},
    )
    return urllib.request.urlopen(req, timeout=1).read().decode()


async def publish_loop(pool, hostname: str):
    """Publish slot utilization to Cloud Monitoring every 30s."""
    # Skip locally — metadata server unreachable off GCE
    try:
        instance_id = _metadata("instance/id")
        zone = _metadata("instance/zone").split("/")[-1]
    except Exception as e:
        print(f"[metric] not running on GCE, skipping: {e}")
        return

    try:
        from google.cloud import monitoring_v3
    except ImportError:
        print("[metric] google-cloud-monitoring not installed, skipping")
        return

    client = monitoring_v3.MetricServiceClient()
    project_name = f"projects/{PROJECT_ID}"
    print(f"[metric] publisher started for instance_id={
          instance_id} zone={zone}")

    while True:
        try:
            used = pool.max_slots - pool.available_count()
            utilization = used / pool.max_slots if pool.max_slots else 0.0

            series = monitoring_v3.TimeSeries()
            series.metric.type = METRIC_TYPE
            series.resource.type = "gce_instance"
            series.resource.labels["instance_id"] = instance_id
            series.resource.labels["zone"] = zone

            now = time.time()
            point = monitoring_v3.Point({
                "interval": monitoring_v3.TimeInterval({"end_time": {"seconds": int(now)}}),
                "value": {"double_value": utilization},
            })
            series.points = [point]

            client.create_time_series(name=project_name, time_series=[series])
            print(f"[metric] slot_utilization={
                  utilization:.2f} ({used}/{pool.max_slots})")
        except Exception as e:
            print(f"[metric] publish failed: {e}")

        await asyncio.sleep(30)
