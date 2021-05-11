from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import (
    SimpleExportSpanProcessor,
)
from typing import Any


def setup_tracing(*args: Any, **kwargs: Any) -> None:
    # Sets the tracer_provider. This is only allowed once per execution
    # context and will log a warning if attempted multiple times.
    trace.set_tracer_provider(TracerProvider())
    trace.get_tracer_provider().add_span_processor(
        SimpleExportSpanProcessor(
            OTLPSpanExporter(
                endpoint="http://localhost:4317",
                insecure=True
                )
        )
    )
