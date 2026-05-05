"""Unit tests for the simplified Coval tracing module.

The module wraps the standard `OTLPSpanExporter` from
`opentelemetry-exporter-otlp-proto-http` and adds a buffer-until-the-Coval-
simulation-id-is-known behavior. These tests cover the buffer/flush logic,
header injection, and the `setup_coval_tracing` entry point. The inner
`OTLPSpanExporter` is mocked so the tests don't make HTTP calls.

End-to-end coverage (real spans hitting `https://api.coval.dev/v1/traces`) is
done by the deployed test-agent and a live Coval simulation — see PR #6 for
the validation details.
"""

from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult

from coval_tracing import COVAL_TRACES_ENDPOINT, DynamicCovalExporter, setup_coval_tracing


# ── setup_coval_tracing ───────────────────────────────────────────────────────


def test_setup_coval_tracing_returns_none_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("COVAL_API_KEY", raising=False)
    assert setup_coval_tracing("test-service") is None


def test_setup_coval_tracing_returns_exporter_when_api_key_present(monkeypatch):
    monkeypatch.setenv("COVAL_API_KEY", "test-key")
    exporter = setup_coval_tracing("test-service")
    assert isinstance(exporter, DynamicCovalExporter)
    assert exporter._api_key == "test-key"
    exporter.shutdown()


# ── DynamicCovalExporter buffer/flush ─────────────────────────────────────────


def test_export_buffers_spans_before_set_simulation_id():
    exporter = DynamicCovalExporter(api_key="test-key")
    spans = [MagicMock(name="span1"), MagicMock(name="span2")]

    result = exporter.export(spans)

    assert result == SpanExportResult.SUCCESS
    assert list(exporter._buffer) == spans
    assert exporter._inner is None


def test_set_simulation_id_constructs_inner_with_correct_headers():
    exporter = DynamicCovalExporter(api_key="test-key")

    with patch("coval_tracing.OTLPSpanExporter") as MockOTLPExporter:
        exporter.set_simulation_id("sim-abc")

    MockOTLPExporter.assert_called_once_with(
        endpoint=COVAL_TRACES_ENDPOINT,
        headers={
            "X-API-Key": "test-key",
            "X-Simulation-Id": "sim-abc",
        },
        timeout=30,
    )


def test_set_simulation_id_flushes_buffered_spans_through_inner_exporter():
    exporter = DynamicCovalExporter(api_key="test-key")
    buffered = [MagicMock(name="span1"), MagicMock(name="span2")]
    exporter.export(buffered)
    assert len(exporter._buffer) == 2

    with patch("coval_tracing.OTLPSpanExporter") as MockOTLPExporter:
        mock_inner = MockOTLPExporter.return_value
        mock_inner.export.return_value = SpanExportResult.SUCCESS
        exporter.set_simulation_id("sim-abc")

    mock_inner.export.assert_called_once_with(buffered)
    assert exporter._buffer == []


def test_set_simulation_id_does_not_call_inner_export_when_buffer_empty():
    exporter = DynamicCovalExporter(api_key="test-key")

    with patch("coval_tracing.OTLPSpanExporter") as MockOTLPExporter:
        mock_inner = MockOTLPExporter.return_value
        exporter.set_simulation_id("sim-abc")

    mock_inner.export.assert_not_called()


def test_export_after_set_simulation_id_goes_directly_to_inner():
    exporter = DynamicCovalExporter(api_key="test-key")

    with patch("coval_tracing.OTLPSpanExporter") as MockOTLPExporter:
        mock_inner = MockOTLPExporter.return_value
        mock_inner.export.return_value = SpanExportResult.SUCCESS
        exporter.set_simulation_id("sim-abc")

        spans = [MagicMock(name="post_set_span")]
        result = exporter.export(spans)

    assert result == SpanExportResult.SUCCESS
    mock_inner.export.assert_called_once_with(spans)
    assert exporter._buffer == []


# ── reset, force_flush, shutdown ──────────────────────────────────────────────


def test_reset_clears_buffer_and_inner():
    exporter = DynamicCovalExporter(api_key="test-key")
    exporter.export([MagicMock(name="span")])

    with patch("coval_tracing.OTLPSpanExporter") as MockOTLPExporter:
        mock_inner = MockOTLPExporter.return_value
        exporter.set_simulation_id("sim-abc")
        exporter.reset()

    assert exporter._inner is None
    assert exporter._buffer == []
    mock_inner.shutdown.assert_called_once()


def test_force_flush_returns_true_when_no_inner():
    exporter = DynamicCovalExporter(api_key="test-key")
    assert exporter.force_flush() is True


def test_force_flush_delegates_to_inner_when_set():
    exporter = DynamicCovalExporter(api_key="test-key")

    with patch("coval_tracing.OTLPSpanExporter") as MockOTLPExporter:
        mock_inner = MockOTLPExporter.return_value
        mock_inner.force_flush.return_value = True
        exporter.set_simulation_id("sim-abc")

        result = exporter.force_flush(timeout_millis=15000)

    assert result is True
    mock_inner.force_flush.assert_called_once_with(15000)


def test_shutdown_no_op_when_no_inner():
    exporter = DynamicCovalExporter(api_key="test-key")
    exporter.shutdown()  # must not raise


def test_shutdown_delegates_to_inner_when_set():
    exporter = DynamicCovalExporter(api_key="test-key")

    with patch("coval_tracing.OTLPSpanExporter") as MockOTLPExporter:
        mock_inner = MockOTLPExporter.return_value
        exporter.set_simulation_id("sim-abc")
        exporter.shutdown()

    mock_inner.shutdown.assert_called_once()


# ── custom endpoint + timeout ─────────────────────────────────────────────────


def test_custom_endpoint_and_timeout_propagated_to_inner():
    exporter = DynamicCovalExporter(
        api_key="test-key",
        endpoint="https://custom.example.com/traces",
        timeout_seconds=60,
    )

    with patch("coval_tracing.OTLPSpanExporter") as MockOTLPExporter:
        exporter.set_simulation_id("sim-abc")

    MockOTLPExporter.assert_called_once_with(
        endpoint="https://custom.example.com/traces",
        headers={"X-API-Key": "test-key", "X-Simulation-Id": "sim-abc"},
        timeout=60,
    )
