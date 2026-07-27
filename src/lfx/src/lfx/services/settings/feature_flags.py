from pydantic_settings import BaseSettings


class FeatureFlags(BaseSettings):
    wxo_deployments: bool = False
    """
    Enable Watsonx Orchestrate deployments.
    """
    on_prem_export: bool = True
    """
    Enable on-prem release export APIs and UI.
    """
    on_prem_builds: bool = False
    """
    Enable building on-prem release artifacts.
    """
    mvp_components: bool = False

    class Config:
        env_prefix = "LANGFLOW_FEATURE_"


FEATURE_FLAGS = FeatureFlags()
