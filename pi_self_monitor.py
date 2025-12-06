#!/usr/bin/env python3
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import psutil

# -------------------- CONFIG --------------------

HOST = "0.0.0.0"
PORT = 8081
POLL_INTERVAL = 10  # seconds

# Systemd services to check for Pi-hole and PiVPN
PIHOLE_SERVICES = ["pihole-FTL.service"]
PIVPN_SERVICES = [
    "wg-quick@wg0.service",      # WireGuard via PiVPN
    "openvpn.service",           # generic OpenVPN
    "openvpn-server@server.service",
]

# -------------------- HELPERS --------------------


def run_cmd(cmd, timeout=5):
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=timeout
        ).decode("utf-8", errors="ignore")
        return out.strip()
    except Exception:
        return ""


def service_active(unit_name):
    if not unit_name:
        return False
    out = run_cmd(["systemctl", "is-active", unit_name])
    return out.strip() == "active"


def get_cpu_temp_c():
    """
    Try Raspberry Pi standard temperature path.
    Returns float or None.
    """
    paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    val = f.read().strip()
                # Usually milli-degrees
                temp_c = float(val) / 1000.0 if float(val) > 200 else float(val)
                return temp_c
            except Exception:
                continue
    return None


def get_uptime_seconds():
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


# -------------------- COLLECTORS --------------------


def collect_system_stats():
    cpu_percent = psutil.cpu_percent(interval=None)
    load1, load5, load15 = os.getloadavg()
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    temp_c = get_cpu_temp_c()
    uptime = get_uptime_seconds()

    return {
        "time": datetime.utcnow().isoformat() + "Z",
        "cpu_percent": cpu_percent,
        "load_1": load1,
        "load_5": load5,
        "load_15": load15,
        "mem_total": mem.total,
        "mem_used": mem.used,
        "mem_percent": mem.percent,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_percent": disk.percent,
        "temp_c": temp_c,
        "uptime_seconds": uptime,
    }


def collect_pihole_stats():
    # service state
    service_ok = any(service_active(s) for s in PIHOLE_SERVICES)

    # stats from pihole -c -j (JSON)
    stats = {}
    raw = run_cmd(["pihole", "-c", "-j"])
    if raw:
        try:
            data = json.loads(raw)
            # commonly available keys, ignore if missing
            for key in [
                "dns_queries_today",
                "ads_blocked_today",
                "ads_percentage_today",
                "domains_being_blocked",
            ]:
                stats[key] = data.get(key)
        except Exception:
            pass

    return {
        "service_ok": service_ok,
        "stats": stats,
    }


def collect_pivpn_stats():
    # Status is "good" if any of the known services is active
    service_ok = any(service_active(s) for s in PIVPN_SERVICES)

    connected_clients = None
    # pivpn -c works for both OpenVPN and WireGuard installs, but may not exist
    raw = run_cmd(["pivpn", "-c"])
    if raw:
        # crude parse: count lines that look like client entries
        # For WireGuard pivpn, header lines and entries
        lines = [l for l in raw.splitlines() if l.strip()]
        # Skip header lines (first 2–3 lines), count the rest
        if len(lines) > 3:
            connected_clients = len(lines) - 3

    return {
        "service_ok": service_ok,
        "connected_clients": connected_clients,
    }


# -------------------- SHARED STATE --------------------

STATE = {
    "system": {},
    "pihole": {},
    "pivpn": {},
}


def update_loop():
    while True:
        try:
            system_stats = collect_system_stats()
            pihole_stats = collect_pihole_stats()
            pivpn_stats = collect_pivpn_stats()

            STATE["system"] = system_stats
            STATE["pihole"] = pihole_stats
            STATE["pivpn"] = pivpn_stats
        except Exception as e:
            # don’t crash the loop - this is a last resort
            STATE["last_error"] = str(e)
        time.sleep(POLL_INTERVAL)


# -------------------- HTTP HANDLER --------------------


def bool_to_status(ok):
    if ok is True:
        return "OK"
    if ok is False:
        return "DOWN"
    return "UNKNOWN"


class PiMonitorHandler(BaseHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        data = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text, status=200, content_type="text/plain"):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self.handle_health()
        elif self.path == "/metrics":
            self.handle_metrics()
        elif self.path == "/":
            self.handle_dashboard()
        else:
            self._send_text("Not found", status=404)

    def handle_health(self):
        sys_s = STATE.get("system", {})
        ph_s = STATE.get("pihole", {})
        pv_s = STATE.get("pivpn", {})

        health = {
            "system": {
                "cpu_percent": sys_s.get("cpu_percent"),
                "mem_percent": sys_s.get("mem_percent"),
                "disk_percent": sys_s.get("disk_percent"),
                "temp_c": sys_s.get("temp_c"),
                "uptime_seconds": sys_s.get("uptime_seconds"),
            },
            "pihole": {
                "status": bool_to_status(ph_s.get("service_ok")),
                "stats": ph_s.get("stats", {}),
            },
            "pivpn": {
                "status": bool_to_status(pv_s.get("service_ok")),
                "connected_clients": pv_s.get("connected_clients"),
            },
            "overall_ok": (
                ph_s.get("service_ok") is True and pv_s.get("service_ok") is True
            ),
            "last_update": sys_s.get("time"),
            "last_error": STATE.get("last_error"),
        }

        self._send_json(health)

    def handle_metrics(self):
        sys_s = STATE.get("system", {})
        ph_s = STATE.get("pihole", {})
        pv_s = STATE.get("pivpn", {})

        stats = ph_s.get("stats", {})

        lines = []

        # System metrics
        lines.append(f'pi_cpu_percent {sys_s.get("cpu_percent", 0)}')
        lines.append(f'pi_load1 {sys_s.get("load_1", 0)}')
        lines.append(f'pi_load5 {sys_s.get("load_5", 0)}')
        lines.append(f'pi_load15 {sys_s.get("load_15", 0)}')
        lines.append(f'pi_mem_percent {sys_s.get("mem_percent", 0)}')
        lines.append(f'pi_disk_percent {sys_s.get("disk_percent", 0)}')
        if sys_s.get("temp_c") is not None:
            lines.append(f'pi_temp_c {sys_s.get("temp_c")}')
        if sys_s.get("uptime_seconds") is not None:
            lines.append(f'pi_uptime_seconds {sys_s.get("uptime_seconds")}')

        # Pi-hole metrics
        lines.append(f'pihole_service_ok {1 if ph_s.get("service_ok") else 0}')
        for k in [
            "dns_queries_today",
            "ads_blocked_today",
            "ads_percentage_today",
            "domains_being_blocked",
        ]:
            v = stats.get(k)
            if v is not None:
                try:
                    val = float(v)
                    lines.append(f'pihole_{k} {val}')
                except (ValueError, TypeError):
                    pass

        # PiVPN metrics
        lines.append(f'pivpn_service_ok {1 if pv_s.get("service_ok") else 0}')
        if pv_s.get("connected_clients") is not None:
            lines.append(f'pivpn_connected_clients {pv_s.get("connected_clients")}')

        body = "\n".join(lines) + "\n"
        self._send_text(body, content_type="text/plain; version=0.0.4")

    def handle_dashboard(self):
        sys_s = STATE.get("system", {})
        ph_s = STATE.get("pihole", {})
        pv_s = STATE.get("pivpn", {})

        def safe_pct(v):
            return f"{v:.1f}%" if isinstance(v, (int, float)) else "N/A"

        temp_disp = (
            f"{sys_s.get('temp_c'):.1f} °C"
            if isinstance(sys_s.get("temp_c"), (int, float))
            else "N/A"
        )

        uptime = sys_s.get("uptime_seconds")
        if uptime is not None:
            days = int(uptime // 86400)
            hours = int((uptime % 86400) // 3600)
            mins = int((uptime % 3600) // 60)
            uptime_str = f"{days}d {hours}h {mins}m"
        else:
            uptime_str = "N/A"

        pihole_status = bool_to_status(ph_s.get("service_ok"))
        pivpn_status = bool_to_status(pv_s.get("service_ok"))

        stats = ph_s.get("stats", {})
        queries = stats.get("dns_queries_today", "N/A")
        blocked = stats.get("ads_blocked_today", "N/A")
        blocked_pct = stats.get("ads_percentage_today", "N/A")

        pv_clients = (
            str(pv_s.get("connected_clients"))
            if pv_s.get("connected_clients") is not None
            else "N/A"
        )

        last_update = sys_s.get("time", "never")
        last_error = STATE.get("last_error")

        html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Pi Self Monitor</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f172a;
      color: #e5e7eb;
      margin: 0;
      padding: 0;
    }}
    .wrap {{
      max-width: 960px;
      margin: 0 auto;
      padding: 1.5rem;
    }}
    h1 {{
      margin-bottom: 0.5rem;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 1rem;
      margin-top: 1rem;
    }}
    .card {{
      background: #111827;
      border-radius: 0.75rem;
      padding: 1rem 1.25rem;
      box-shadow: 0 10px 25px rgba(0,0,0,0.4);
      border: 1px solid #1f2937;
    }}
    .card h2 {{
      margin-top: 0;
      font-size: 1.05rem;
      margin-bottom: 0.75rem;
    }}
    .kv {{
      display: flex;
      justify-content: space-between;
      margin: 0.2rem 0;
      font-size: 0.95rem;
    }}
    .label {{
      color: #9ca3af;
    }}
    .value {{
      font-weight: 500;
    }}
    .status-ok {{
      color: #22c55e;
      font-weight: 600;
    }}
    .status-down {{
      color: #ef4444;
      font-weight: 600;
    }}
    .status-unknown {{
      color: #f97316;
      font-weight: 600;
    }}
    .footer {{
      margin-top: 1rem;
      font-size: 0.8rem;
      color: #6b7280;
    }}
    a {{
      color: #60a5fa;
    }}
  </style>
</head>
<body>
<div class="wrap">
  <h1>Pi Self Monitor</h1>
  <div class="footer">
    Last update: {last_update} • Uptime: {uptime_str}
    {"• Last error: " + last_error if last_error else ""}
    <br>
    API endpoints: <a href="/health">/health</a> • <a href="/metrics">/metrics</a>
  </div>
  <div class="grid">
    <div class="card">
      <h2>System</h2>
      <div class="kv"><span class="label">CPU usage</span><span class="value">{safe_pct(sys_s.get("cpu_percent"))}</span></div>
      <div class="kv"><span class="label">Load (1/5/15)</span><span class="value">{sys_s.get("load_1", "N/A")} / {sys_s.get("load_5", "N/A")} / {sys_s.get("load_15", "N/A")}</span></div>
      <div class="kv"><span class="label">Memory</span><span class="value">{safe_pct(sys_s.get("mem_percent"))}</span></div>
      <div class="kv"><span class="label">Disk /</span><span class="value">{safe_pct(sys_s.get("disk_percent"))}</span></div>
      <div class="kv"><span class="label">Temperature</span><span class="value">{temp_disp}</span></div>
    </div>
    <div class="card">
      <h2>Pi-hole</h2>
      <div class="kv"><span class="label">Service</span>
        <span class="value {"status-ok" if pihole_status=="OK" else "status-down" if pihole_status=="DOWN" else "status-unknown"}">{pihole_status}</span>
      </div>
      <div class="kv"><span class="label">Queries today</span><span class="value">{queries}</span></div>
      <div class="kv"><span class="label">Blocked</span><span class="value">{blocked}</span></div>
      <div class="kv"><span class="label">Blocked %</span><span class="value">{blocked_pct}</span></div>
    </div>
    <div class="card">
      <h2>PiVPN</h2>
      <div class="kv"><span class="label">Service</span>
        <span class="value {"status-ok" if pivpn_status=="OK" else "status-down" if pivpn_status=="DOWN" else "status-unknown"}">{pivpn_status}</span>
      </div>
      <div class="kv"><span class="label">Connected clients</span><span class="value">{pv_clients}</span></div>
    </div>
  </div>
</div>
</body>
</html>"""

        self._send_text(html, content_type="text/html; charset=utf-8")



def main():
    # background collector
    t = threading.Thread(target=update_loop, daemon=True)
    t.start()

    server = HTTPServer((HOST, PORT), PiMonitorHandler)
    print(f"Pi Self Monitor listening on {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
