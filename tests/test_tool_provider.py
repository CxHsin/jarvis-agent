import pytest

from tools.tool_runtime import (
    ProviderCapabilityMode, ProviderLoadError, ProviderSession,
    ToolProviderAdapter, ToolRegistry, ToolRuntime,
)


def adapter(mode="native"):
    return ToolProviderAdapter(ToolRuntime(ToolRegistry()), ProviderSession(mode))


def test_native_success_keeps_session_mode():
    provider = adapter()
    references = [{"tool_id": "read", "version": "1"}]
    assert provider.load("task", lambda: references) == references
    assert provider.session.native_deferred
    assert provider.session.fallback_count == 0


def test_three_eligible_failures_fall_back_once_for_session():
    provider = adapter()
    calls = []

    def native():
        calls.append("native")
        raise ProviderLoadError("network unavailable")

    def emulated():
        calls.append("emulated")
        return ["schema"]

    assert provider.load("task", native, emulated_loader=emulated) == ["schema"]
    assert calls == ["native", "native", "native", "emulated"]
    assert provider.session.mode is ProviderCapabilityMode.EMULATED
    assert provider.load("next-task", native, emulated_loader=emulated) == ["schema"]
    assert calls[-1] == "emulated"
    assert calls.count("native") == 3
    assert provider.session.fallback_count == 1


def test_ineligible_failure_does_not_retry_or_fallback():
    provider = adapter()
    calls = []

    def native():
        calls.append("native")
        raise ProviderLoadError("bad arguments", eligible=False)

    with pytest.raises(ProviderLoadError):
        provider.load("task", native)
    assert calls == ["native"]
    assert provider.session.native_deferred


def test_emulated_mode_never_calls_native():
    provider = adapter("emulated")
    assert provider.load("task", lambda: pytest.fail("native was called")) == []


def test_native_response_is_not_mixed_with_fallback():
    provider = adapter()

    def native():
        partial = [{"type": "tool_reference", "name": "partial"}]
        raise ProviderLoadError(str(partial))

    assert provider.load("task", native, emulated_loader=lambda: ["schema"]) == ["schema"]
