import type { useMutationFunctionType } from "@/types/api";
import { api } from "../../api";
import { getURL } from "../../helpers/constants";
import { UseRequestProcessor } from "../../services/request-processor";
import type {
  OnPremBuild,
  OnPremExportResult,
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
