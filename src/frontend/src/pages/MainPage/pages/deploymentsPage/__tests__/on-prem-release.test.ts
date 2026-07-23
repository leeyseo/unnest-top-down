import {
  buildOnPremReleasePayload,
  defaultOnPremWizardValues,
} from "../helpers/on-prem-release";

describe("on-prem release payload", () => {
  it("normalizes deployment choices without putting secret values in the payload", () => {
    const payload = buildOnPremReleasePayload({
      ...defaultOnPremWizardValues,
      agentFlowVersionId: "agent-version",
      ingestionFlowVersionId: "ingestion-version",
      accelerator: "nvidia",
      gpuCount: 2,
      storeConversations: true,
      externalEndpoints: "https://model.internal\nhttps://vector.internal",
      secretNames: "MODEL_TOKEN, STORAGE_KEY",
      apiContract: JSON.stringify({
        input_schema: { type: "object" },
        output_schema: { type: "object" },
        request_example: {},
        response_example: {},
        input_mapping: {},
        output_mapping: {},
      }),
    });

    expect(payload.agent_flow_version_id).toBe("agent-version");
    expect(payload.ingestion_flow_version_id).toBe("ingestion-version");
    expect(payload.config).toMatchObject({
      accelerator: "nvidia",
      conversation_retention_days: 30,
      external_endpoints: ["https://model.internal", "https://vector.internal"],
      additional_secret_names: ["MODEL_TOKEN", "STORAGE_KEY"],
      resources: { gpu_count: 2 },
    });
    expect(JSON.stringify(payload)).not.toContain("secret_value");
  });

  it("omits conversation retention and GPU allocation when disabled", () => {
    const payload = buildOnPremReleasePayload(defaultOnPremWizardValues);

    expect(payload.config).toMatchObject({
      store_conversations: false,
      conversation_retention_days: null,
      resources: { gpu_count: 0 },
    });
  });
});
