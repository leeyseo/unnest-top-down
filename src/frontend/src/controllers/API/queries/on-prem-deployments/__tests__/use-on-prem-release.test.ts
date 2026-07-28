const mockApiGet = jest.fn();
const mockApiPost = jest.fn();
const mockInvalidateQueries = jest.fn();

jest.mock("@/controllers/API/api", () => ({
  api: { get: mockApiGet, post: mockApiPost },
}));

jest.mock("@/controllers/API/helpers/constants", () => ({
  getURL: jest.fn(() => "/api/v1/deployments"),
}));

jest.mock("@/controllers/API/services/request-processor", () => ({
  UseRequestProcessor: jest.fn(() => ({
    // biome-ignore lint/suspicious/noExplicitAny: test mutation adapter
    mutate: jest.fn((_key: unknown, fn: any, options: any) => {
      // biome-ignore lint/suspicious/noExplicitAny: test mutation adapter
      const mutateAsync = async (payload: any) => {
        const result = await fn(payload);
        await options?.onSuccess?.(result);
        return result;
      };
      return { mutate: mutateAsync, mutateAsync };
    }),
    queryClient: { invalidateQueries: mockInvalidateQueries },
  })),
}));

import type { OnPremReleasePayload } from "../types";
import {
  getOnPremArtifactDownloadUrl,
  useExportOnPremRelease,
  usePushOnPremRegistry,
  useSyncOnPremBuild,
  useValidateOnPremRelease,
} from "../use-on-prem-release";

const payload = {
  release_version: "1.0.0",
  agent_flow_version_id: "agent",
  ingestion_flow_version_id: "ingestion",
  source_file_ids: [],
  config: {},
  api: {},
  acceptance_tests: [],
} satisfies OnPremReleasePayload;

describe("on-prem release API", () => {
  beforeEach(() => jest.clearAllMocks());

  it("validates without creating a release", async () => {
    mockApiPost.mockResolvedValue({
      data: { manifest: {}, errors: [], warnings: [] },
    });

    const mutation = useValidateOnPremRelease();
    await mutation.mutate(payload);

    expect(mockApiPost).toHaveBeenCalledWith(
      "/api/v1/deployments/on-prem/releases/validate",
      payload,
    );
  });

  it("creates a release and submits its pending build", async () => {
    mockApiPost
      .mockResolvedValueOnce({
        data: {
          id: "release-1",
          release_version: "1.0.0",
          api_version: "v1",
          warnings: [],
        },
      })
      .mockResolvedValueOnce({
        data: {
          id: "build-1",
          release_id: "release-1",
          architecture: "amd64",
          status: "queued",
        },
      });
    mockApiGet.mockResolvedValue({
      data: {
        builds: [
          {
            id: "build-1",
            release_id: "release-1",
            architecture: "amd64",
            status: "pending",
          },
        ],
      },
    });

    const mutation = useExportOnPremRelease();
    const result = await mutation.mutateAsync(payload);

    expect(mockApiGet).toHaveBeenCalledWith(
      "/api/v1/deployments/on-prem/releases/release-1/builds",
    );
    expect(mockApiPost).toHaveBeenLastCalledWith(
      "/api/v1/deployments/on-prem/releases/release-1/builds/build-1/submit",
    );
    expect(result.build.status).toBe("queued");
    expect(mockInvalidateQueries).toHaveBeenCalledWith({
      queryKey: ["useGetOnPremReleases"],
    });
  });

  it("syncs builds and pushes using a secret reference only", async () => {
    mockApiPost
      .mockResolvedValueOnce({ data: { id: "build-1", status: "succeeded" } })
      .mockResolvedValueOnce({
        data: {
          id: "artifact-1",
          location: "registry.internal/agency/unnest:1.0.0",
        },
      });

    const sync = useSyncOnPremBuild();
    await sync.mutate({ releaseId: "release-1", buildId: "build-1" });
    const push = usePushOnPremRegistry();
    await push.mutate({
      releaseId: "release-1",
      buildId: "build-1",
      reference: "registry.internal/agency/unnest:1.0.0",
      credentialSecretName: "REGISTRY_CREDENTIAL",
    });

    expect(mockApiPost).toHaveBeenNthCalledWith(
      1,
      "/api/v1/deployments/on-prem/releases/release-1/builds/build-1/sync",
    );
    expect(mockApiPost).toHaveBeenNthCalledWith(
      2,
      "/api/v1/deployments/on-prem/releases/release-1/builds/build-1/registry",
      {
        reference: "registry.internal/agency/unnest:1.0.0",
        credential_secret_name: "REGISTRY_CREDENTIAL",
      },
    );
    expect(
      getOnPremArtifactDownloadUrl("release-1", "build-1", "artifact-1"),
    ).toBe(
      "/api/v1/deployments/on-prem/releases/release-1/builds/build-1/artifacts/artifact-1/download",
    );
  });
});
