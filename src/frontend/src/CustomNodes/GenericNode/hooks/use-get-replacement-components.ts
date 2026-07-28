import { useGetCurrentComponentVisibility } from "@/controllers/API/queries/auth";
import { useTypesStore } from "@/stores/typesStore";
import { isComponentVisible } from "@/utils/component-visibility";

export const useGetReplacementComponents = (replacement?: string[]) => {
  const data = useTypesStore((state) => state.data);
  const { data: componentVisibility } = useGetCurrentComponentVisibility();

  return replacement && Array.isArray(replacement) && replacement.length > 0
    ? replacement.map((component) => {
        const categoryName = component?.split(".")[0];
        const componentName = component?.split(".")[1];

        if (
          !categoryName ||
          !componentName ||
          !isComponentVisible(categoryName, componentName, componentVisibility)
        ) {
          return undefined;
        }

        return data[categoryName]?.[componentName]?.display_name;
      })
    : [];
};
