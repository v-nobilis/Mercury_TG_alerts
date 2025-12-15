Telegram alerts: https://t.me/mercuryo_alerts

Grafana: https://nobilis.grafana.net/public-dashboards/8cb245b6532d40cc8debc3a851d93411
queries:
from(bucket: "monitor_data")
  |> range(start: 2025-12-08T09:50:00Z)
  |> filter(fn: (r) => r["_measurement"] == "spread_monitor")
  |> filter(fn: (r) => r["_field"] == "spread_pct")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> yield(name: "mean")
  
