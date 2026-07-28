import langflow.services.utils as service_utils
from lfx.services.settings.feature_flags import FeatureFlags


def test_deployment_feature_flag_defaults(monkeypatch):
    for name in (
        "LANGFLOW_FEATURE_WXO_DEPLOYMENTS",
        "LANGFLOW_FEATURE_ON_PREM_EXPORT",
        "LANGFLOW_FEATURE_ON_PREM_BUILDS",
    ):
        monkeypatch.delenv(name, raising=False)

    flags = FeatureFlags()

    assert flags.wxo_deployments is False
    assert flags.on_prem_export is True
    assert flags.on_prem_builds is False


def test_register_builtin_adapters_skips_import_when_feature_disabled(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(service_utils.FEATURE_FLAGS, "wxo_deployments", False)
    monkeypatch.setattr(service_utils, "import_module", lambda name: calls.append(name))

    service_utils.register_builtin_adapters()

    assert calls == []


def test_register_builtin_adapters_imports_when_feature_enabled(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(service_utils.FEATURE_FLAGS, "wxo_deployments", True)
    monkeypatch.setattr(service_utils, "import_module", lambda name: calls.append(name))

    service_utils.register_builtin_adapters()

    assert calls == ["langflow.services.adapters.deployment.watsonx_orchestrate.register"]


def test_register_builtin_deployment_mappers_skips_import_when_feature_disabled(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(service_utils.FEATURE_FLAGS, "wxo_deployments", False)
    monkeypatch.setattr(service_utils, "import_module", lambda name: calls.append(name))

    service_utils.register_builtin_deployment_mappers()

    assert calls == []


def test_register_builtin_deployment_mappers_imports_when_feature_enabled(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(service_utils.FEATURE_FLAGS, "wxo_deployments", True)
    monkeypatch.setattr(service_utils, "import_module", lambda name: calls.append(name))

    service_utils.register_builtin_deployment_mappers()

    assert calls == ["langflow.api.v1.mappers.deployments.watsonx_orchestrate"]
