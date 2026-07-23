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
    mutate: jest.fn((_key: unknown, fn: any, options: any) => ({
      // biome-ignore lint/suspicious/noExplicitAny: test mutation adapter
      mutate: async (payload: any) => {
        const result = await fn(payload);
        await options?.onSuccess?.(result);
        return result;
      },
    })),
    queryClient: { invalidateQueries: mockInvalidateQueries },
  })),
}));

import type { OnPremReleasePayload } from "../types";
import {
  useExportOnPremRelease,
  useValidateOnPremRelease,
} from "../use-on-prem-release";

const payload = {
  release_version: "1.0.0",
  agent_flow_version_id: "agent",
  ingestion_flow_version_id: "ingestion",
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
    const result = await mutation.mutate(payload);

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
});
