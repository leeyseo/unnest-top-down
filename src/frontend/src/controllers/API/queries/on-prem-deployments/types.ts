export type OnPremReleasePayload = {
  release_version: string;
  agent_flow_version_id: string;
  ingestion_flow_version_id: string;
  config: Record<string, unknown>;
  api: Record<string, unknown>;
  acceptance_tests: unknown[];
};

export type OnPremReleaseValidation = {
  manifest: Record<string, unknown> | null;
  errors: string[];
  warnings: string[];
};

export type OnPremRelease = {
  id: string;
  release_version: string;
  api_version: string;
  warnings: string[];
};

export type OnPremBuild = {
  id: string;
  release_id: string;
  architecture: string;
  status: string;
  artifacts: OnPremArtifact[];
};

export type OnPremArtifact = {
  id: string;
  build_id: string;
  artifact_type: string;
  location: string;
  digest: string;
  size_bytes: number;
  signature: string | null;
};

export type OnPremExportResult = {
  release: OnPremRelease;
  build: OnPremBuild;
};

export type OnPremBuildTarget = {
  releaseId: string;
  buildId: string;
};

export type OnPremRegistryPush = OnPremBuildTarget & {
  reference: string;
  credentialSecretName: string;
};
