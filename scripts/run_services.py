"""Start the four MCP servers and the MCP Gateway as separate processes.

Only needed when SPLIT_PROCESSES=true. With the default single-process mode the
FastAPI app calls the gateway and the MCP servers in-process and this script is
not used.

    python scripts/run_services.py

Ctrl-C stops everything.
"""

import signal
import subprocess
import sys
import time

SERVICES = [
    ("transport-mcp", [sys.executable, "-m", "mcp_servers.transport.server"]),
    ("hotels-mcp", [sys.executable, "-m", "mcp_servers.hotels.server"]),
    ("places-mcp", [sys.executable, "-m", "mcp_servers.places.server"]),
    ("weather-mcp", [sys.executable, "-m", "mcp_servers.weather.server"]),
    ("gateway", [sys.executable, "-m", "uvicorn", "gateway.server:app", "--port", "9000"]),
]


def main() -> int:
    processes = []
    for name, command in SERVICES:
        print(f"starting {name}: {' '.join(command)}", flush=True)
        processes.append((name, subprocess.Popen(command)))

    def shutdown(*_):
        for name, process in processes:
            print(f"stopping {name}", flush=True)
            process.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # If any service dies, take the rest down so the failure is obvious.
    while True:
        for name, process in processes:
            if process.poll() is not None:
                print(f"{name} exited with {process.returncode}", flush=True)
                shutdown()
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
