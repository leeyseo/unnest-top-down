import type { useMutationFunctionType } from "@/types/api";
import { api } from "../../api";
import { getURL } from "../../helpers/constants";
import { UseRequestProcessor } from "../../services/request-processor";
import type {
  OnPremArtifact,
  OnPremBuild,
  OnPremBuildTarget,
  OnPremExportResult,
  OnPremRegistryPush,
  OnPremRelease,
  OnPremReleasePayload,
  OnPremReleaseValidation,
} from "./types";

const releasesUrl = `${getURL("DEPLOYMENTS")}/on-prem/releases`;

export const useValidateOnPremRelease: useMutationFunctionType<
  undefined,
  OnPremReleasePayload,
  OnPremReleaseValidation
> = (options?) => {
  const { mutate } = UseRequestProcessor();
  return mutate(
    ["useValidateOnPremRelease"],
    async (payload: OnPremReleasePayload) =>
      (
        await api.post<OnPremReleaseValidation>(
          `${releasesUrl}/validate`,
          payload,
        )
      ).data,
    { ...options, retry: 0 },
  );
};

export const useExportOnPremRelease: useMutationFunctionType<
  undefined,
  OnPremReleasePayload,
  OnPremExportResult
> = (options?) => {
  const { mutate, queryClient } = UseRequestProcessor();

  return mutate(
    ["useExportOnPremRelease"],
    async (payload: OnPremReleasePayload): Promise<OnPremExportResult> => {
      const release = (await api.post<OnPremRelease>(releasesUrl, payload))
        .data;
      const builds = (
        await api.get<{ builds: OnPremBuild[] }>(
          `${releasesUrl}/${release.id}/builds`,
        )
      ).data.builds;
      const build = builds.find((item) => item.status === "pending");
      if (!build) throw new Error("The release did not create a pending build");
      const submitted = (
        await api.post<OnPremBuild>(
          `${releasesUrl}/${release.id}/builds/${build.id}/submit`,
        )
      ).data;
      return { release, build: submitted };
    },
    {
      ...options,
      retry: 0,
      onSuccess: (...args) => {
        queryClient.invalidateQueries({ queryKey: ["useGetOnPremReleases"] });
        options?.onSuccess?.(...args);
      },
    },
  );
};

export const useSyncOnPremBuild: useMutationFunctionType<
  undefined,
  OnPremBuildTarget,
  OnPremBuild
> = (options?) => {
  const { mutate } = UseRequestProcessor();
  return mutate(
    ["useSyncOnPremBuild"],
    async ({ releaseId, buildId }: OnPremBuildTarget) =>
      (
        await api.post<OnPremBuild>(
          `${releasesUrl}/${releaseId}/builds/${buildId}/sync`,
        )
      ).data,
    { ...options, retry: 0 },
  );
};

export const usePushOnPremRegistry: useMutationFunctionType<
  undefined,
  OnPremRegistryPush,
  OnPremArtifact
> = (options?) => {
  const { mutate } = UseRequestProcessor();
  return mutate(
    ["usePushOnPremRegistry"],
    async ({
      releaseId,
      buildId,
      reference,
      credentialSecretName,
    }: OnPremRegistryPush) =>
      (
        await api.post<OnPremArtifact>(
          `${releasesUrl}/${releaseId}/builds/${buildId}/registry`,
          {
            reference,
            credential_secret_name: credentialSecretName,
          },
        )
      ).data,
    { ...options, retry: 0 },
  );
};

export function getOnPremArtifactDownloadUrl(
  releaseId: string,
  buildId: string,
  artifactId: string,
) {
  return `${releasesUrl}/${releaseId}/builds/${buildId}/artifacts/${artifactId}/download`;
}
