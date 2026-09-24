"""Standard OTLP/HTTP protobuf, exported off the tool execution path."""

import logging
import os
import uuid

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from . import __version__
from .config import validate_url
from .capture import Capture

LOG = logging.getLogger("mcp_otel_gateway")


def make_exporter():
    protocol = os.getenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
                         os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"))
    if protocol != "http/protobuf":
        raise ValueError("This gateway exports http/protobuf; use an OTel Collector for translation")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        raise ValueError("Set OTEL_EXPORTER_OTLP_ENDPOINT or OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    validate_url(endpoint)
    return OTLPSpanExporter()


class CheckedExporter(SpanExporter):
    def __init__(self, delegate: SpanExporter):
        self.delegate = delegate

    def export(self, spans):
        try:
            result = self.delegate.export(spans)
        except Exception:
            result = SpanExportResult.FAILURE
        if result != SpanExportResult.SUCCESS:
            LOG.warning("Telemetry batch export failed; tool execution is unaffected. Check the collector.")
        return result

    def shutdown(self):
        self.delegate.shutdown()


class Telemetry:
    def __init__(self, upstream_name: str, agent_name: str, exporter: SpanExporter | None = None,
                 *, export_logs=False, log_exporter=None):
        if exporter is None:
            exporter = make_exporter()
        resource = Resource.create({
            "service.name": os.getenv("OTEL_SERVICE_NAME", "mcp-otel-gateway"),
            "service.version": __version__,
            "service.instance.id": str(uuid.uuid4()),
            "agent_monitor.upstream.name": upstream_name,
            "gen_ai.agent.name": agent_name,
            "agent_monitor.coverage": "gateway_tool_calls_only",
        })
        # Keep this provider private: SDK auto-instrumentation must not capture payloads.
        # Record all gateway calls even when an incoming parent is unsampled.
        self.provider = TracerProvider(resource=resource, sampler=ALWAYS_ON)
        self.provider.add_span_processor(BatchSpanProcessor(
            CheckedExporter(exporter), max_queue_size=2048,
            max_export_batch_size=128, schedule_delay_millis=1000,
        ))
        self.tracer = self.provider.get_tracer("txy.mcp.gateway", __version__)
        self.log_provider = None
        self.log_logger = None
        self.log_capture = Capture(32768) if export_logs else None
        if export_logs:
            from opentelemetry.sdk._logs import LoggerProvider
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            if log_exporter is None:
                endpoint = os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
                if not endpoint:
                    raise ValueError("Log export needs a base endpoint or exact logs endpoint")
                validate_url(endpoint)
                protocol = os.getenv("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"))
                if protocol != "http/protobuf":
                    raise ValueError("Logs require http/protobuf")
                log_exporter = OTLPLogExporter()
            self.log_provider = LoggerProvider(resource=resource)
            self.log_provider.add_log_record_processor(BatchLogRecordProcessor(
                log_exporter, max_queue_size=2048, max_export_batch_size=128,
                schedule_delay_millis=1000,
            ))
            self.log_logger = self.log_provider.get_logger("txy.mcp.gateway", __version__)

    def log(self, event, tool, content=None, *, error=False):
        if self.log_logger is None:
            return
        try:
            from opentelemetry._logs import SeverityNumber
            body, truncated = self.log_capture.encode({"event": event, "tool": tool, "content": content})
            self.log_logger.emit(body=body, severity_number=SeverityNumber.ERROR if error else SeverityNumber.INFO,
                attributes={"event.name": event, "gen_ai.tool.name": self.log_capture.text(tool)[:256],
                            "agent_monitor.evidence.source": "gateway_observed",
                            "agent_monitor.content.truncated": truncated,
                            "agent_monitor.redaction": "best_effort"})
        except Exception:
            LOG.warning("Log capture failed; tool execution is unaffected.")

    def close(self):
        if self.log_provider:
            self.log_provider.force_flush(timeout_millis=5000)
            self.log_provider.shutdown()
        self.provider.force_flush(timeout_millis=5000)
        self.provider.shutdown()
