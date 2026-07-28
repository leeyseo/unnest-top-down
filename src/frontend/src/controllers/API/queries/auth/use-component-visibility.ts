import type {
  UseMutationResult,
  UseQueryOptions,
  UseQueryResult,
} from "@tanstack/react-query";
import type { ComponentVisibility } from "@/types/api";
import { api } from "../../api";
import { getURL } from "../../helpers/constants";
import { UseRequestProcessor } from "../../services/request-processor";

type VisibilityUpdateParams = {
  userId: string;
  visibility: Pick<ComponentVisibility, "hidden_bundles" | "hidden_components">;
};

type VisibilityQueryOptions = Omit<UseQueryOptions, "queryKey" | "queryFn">;

const getVisibility = async (path: string): Promise<ComponentVisibility> => {
  const response = await api.get<ComponentVisibility>(path);
  return response.data;
};

export function useGetCurrentComponentVisibility(
  options?: VisibilityQueryOptions,
): UseQueryResult<ComponentVisibility> {
  const { query } = UseRequestProcessor();
  return query(
    ["useGetCurrentComponentVisibility"],
    () => getVisibility(`${getURL("USERS")}/whoami/component-visibility`),
    { refetchOnWindowFocus: false, ...options },
  ) as UseQueryResult<ComponentVisibility>;
}

export function useGetUserComponentVisibility(
  userId?: string,
  options?: VisibilityQueryOptions,
): UseQueryResult<ComponentVisibility> {
  const { query } = UseRequestProcessor();
  return query(
    ["useGetUserComponentVisibility", userId],
    () => getVisibility(`${getURL("USERS")}/${userId}/component-visibility`),
    { refetchOnWindowFocus: false, enabled: Boolean(userId), ...options },
  ) as UseQueryResult<ComponentVisibility>;
}

export function useUpdateComponentVisibility(): UseMutationResult<
  ComponentVisibility,
  unknown,
  VisibilityUpdateParams
> {
  const { mutate, queryClient } = UseRequestProcessor();

  const updateVisibility = async ({
    userId,
    visibility,
  }: VisibilityUpdateParams): Promise<ComponentVisibility> => {
    const response = await api.put<ComponentVisibility>(
      `${getURL("USERS")}/${userId}/component-visibility`,
      visibility,
    );
    return response.data;
  };

  return mutate(["useUpdateComponentVisibility"], updateVisibility, {
    onSuccess: (visibility: ComponentVisibility) => {
      queryClient.setQueryData(
        ["useGetUserComponentVisibility", visibility.user_id],
        visibility,
      );
    },
  }) as UseMutationResult<ComponentVisibility, unknown, VisibilityUpdateParams>;
}
