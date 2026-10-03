"""Create a private Grafana Cloud OTLP configuration for the demo.

Run from a clone: python3 examples/grafana/configure.py
"""

import argparse
import base64
import getpass
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=Path.home() / ".config/mcp-otel-gateway/grafana.json",
        help="Private config path (default: ~/.config/mcp-otel-gateway/grafana.json)",
    )
    args = parser.parse_args()
    endpoint = input("Grafana Cloud OTLP endpoint (ends in /otlp): ").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.query or parsed.path != "/otlp"):
        parser.error("Enter the HTTPS OTLP base endpoint from Grafana's OpenTelemetry card, ending in /otlp")
    instance_id = input("OTLP instance ID from the same card: ").strip()
    if not instance_id.isdecimal():
        parser.error("The OTLP instance ID must be the number shown on the OpenTelemetry card")
    token = getpass.getpass("Cloud Access Policy token (traces:write and logs:write): ").strip()
    if not token:
        parser.error("A Cloud Access Policy token is required")
    basic = base64.b64encode(f"{instance_id}:{token}".encode()).decode("ascii")
    config = {
        "OTEL_SERVICE_NAME": "mcp-otel-gateway",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Basic%20{basic}",
    }
    target = args.output.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        parser.error("Refusing to write credentials through a symlink")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error(f"Config already exists at {target}; choose a new --output path to rotate credentials")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream)
        stream.write("\n")
    print(f"Saved private OTLP settings to {target} (mode 0600).")
    print("Run: .venv/bin/python examples/grafana/smoke.py")


if __name__ == "__main__":
    main()
