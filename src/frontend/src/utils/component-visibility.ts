import type { APIObjectType, ComponentVisibility } from "@/types/api";

export const componentVisibilityKey = (bundle: string, component: string) =>
  `${bundle}.${component}`;

export const isComponentVisible = (
  bundle: string,
  component: string,
  visibility?: ComponentVisibility,
) =>
  !visibility?.hidden_bundles.includes(bundle) &&
  !visibility?.hidden_components.includes(
    componentVisibilityKey(bundle, component),
  );

export const filterComponentCatalog = (
  data: APIObjectType,
  visibility?: ComponentVisibility,
): APIObjectType => {
  if (!visibility) return data;

  return Object.fromEntries(
    Object.entries(data)
      .filter(([bundle]) => !visibility.hidden_bundles.includes(bundle))
      .map(([bundle, components]) => [
        bundle,
        Object.fromEntries(
          Object.entries(components).filter(([component]) =>
            isComponentVisible(bundle, component, visibility),
          ),
        ),
      ]),
  );
};
