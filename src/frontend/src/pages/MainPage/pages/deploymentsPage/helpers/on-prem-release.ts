import type { OnPremReleasePayload } from "@/controllers/API/queries/on-prem-deployments/types";

export type OnPremWizardValues = {
  releaseVersion: string;
  agentFlowVersionId: string;
  ingestionFlowVersionId: string;
  sourceFileIds: string[];
  orchestrator: "compose" | "helm";
  topology: "single" | "ha";
  architecture: "amd64" | "arm64";
  accelerator: "cpu" | "nvidia" | "amd";
  database: "embedded-postgresql" | "external-postgresql";
  storage: "local" | "minio" | "s3";
  infrastructure: "bundled" | "external";
  modelRuntime: "external" | "bundled";
  includeModelWeights: boolean;
  storeConversations: boolean;
  conversationRetentionDays: number;
  tls: "institution" | "self-signed";
  defaultLanguage: "ko" | "en";
  allowLanguageSwitch: boolean;
  solutionName: string;
  organizationName: string;
  logoUrl: string;
  primaryColor: string;
  loginNotice: string;
  showUnnestBranding: boolean;
  signing: boolean;
  backups: boolean;
  monitoring: boolean;
  clamav: boolean;
  grafana: boolean;
  retentionDays: number;
  retentionMaxGiB: number;
  pinned: boolean;
  cpu: number;
  memoryGiB: number;
  diskGiB: number;
  gpuCount: number;
  externalEndpoints: string;
  secretNames: string;
  apiContract: string;
};

export type OnPremWizardUpdate = <K extends keyof OnPremWizardValues>(
  key: K,
  value: OnPremWizardValues[K],
) => void;

export const defaultApiContract = JSON.stringify(
  {
    input_schema: {
      type: "object",
      properties: { input: { type: "string" } },
      required: ["input"],
      additionalProperties: false,
    },
    output_schema: {
      type: "object",
      properties: { output: { type: "string" } },
      required: ["output"],
      additionalProperties: false,
    },
    request_example: { input: "안녕하세요" },
    response_example: { output: "안녕하세요" },
    input_mapping: {
      input: { component_id: "", component_field: "input_value" },
    },
    output_mapping: {
      output: { component_id: "", result_path: "result" },
    },
  },
  null,
  2,
);

export const defaultOnPremWizardValues: OnPremWizardValues = {
  releaseVersion: "1.0.0",
  agentFlowVersionId: "",
  ingestionFlowVersionId: "",
  sourceFileIds: [],
  orchestrator: "compose",
  topology: "single",
  architecture: "amd64",
  accelerator: "cpu",
  database: "embedded-postgresql",
  storage: "local",
  infrastructure: "bundled",
  modelRuntime: "external",
  includeModelWeights: false,
  storeConversations: false,
  conversationRetentionDays: 30,
  tls: "self-signed",
  defaultLanguage: "ko",
  allowLanguageSwitch: true,
  solutionName: "Unnest",
  organizationName: "",
  logoUrl: "",
  primaryColor: "#3f6212",
  loginNotice: "",
  showUnnestBranding: true,
  signing: true,
  backups: true,
  monitoring: true,
  clamav: false,
  grafana: false,
  retentionDays: 30,
  retentionMaxGiB: 20,
  pinned: false,
  cpu: 2,
  memoryGiB: 4,
  diskGiB: 20,
  gpuCount: 0,
  externalEndpoints: "",
  secretNames: "",
  apiContract: defaultApiContract,
};

const lines = (value: string) =>
  value
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);

export function buildOnPremReleasePayload(
  values: OnPremWizardValues,
): OnPremReleasePayload {
  return {
    release_version: values.releaseVersion.trim(),
    agent_flow_version_id: values.agentFlowVersionId,
    ingestion_flow_version_id: values.ingestionFlowVersionId,
    source_file_ids: values.sourceFileIds,
    config: {
      orchestrator: values.orchestrator,
      topology: values.topology,
      architecture: values.architecture,
      accelerator: values.accelerator,
      database: values.database,
      storage: values.storage,
      infrastructure: values.infrastructure,
      model_runtime: values.modelRuntime,
      include_model_weights: values.includeModelWeights,
      store_conversations: values.storeConversations,
      conversation_retention_days: values.storeConversations
        ? values.conversationRetentionDays
        : null,
      tls: values.tls,
      default_language: values.defaultLanguage,
      allow_language_switch: values.allowLanguageSwitch,
      external_endpoints: lines(values.externalEndpoints),
      additional_secret_names: lines(values.secretNames),
      branding: {
        solution_name: values.solutionName.trim(),
        organization_name: values.organizationName.trim(),
        logo_url: values.logoUrl.trim() || null,
        primary_color: values.primaryColor,
        login_notice: values.loginNotice,
        show_unnest_branding: values.showUnnestBranding,
      },
      features: {
        signing: values.signing,
        backups: values.backups,
        monitoring: values.monitoring,
        clamav: values.clamav,
        grafana: values.grafana,
      },
      retention: {
        days: values.retentionDays,
        max_bytes: values.retentionMaxGiB * 1024 ** 3,
        pinned: values.pinned,
      },
      resources: {
        cpu: values.cpu,
        memory_bytes: values.memoryGiB * 1024 ** 3,
        disk_bytes: values.diskGiB * 1024 ** 3,
        gpu_count: values.accelerator === "cpu" ? 0 : values.gpuCount,
      },
    },
    api: JSON.parse(values.apiContract) as Record<string, unknown>,
    acceptance_tests: [],
  };
}
