import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  useGetUserComponentVisibility,
  useUpdateComponentVisibility,
} from "@/controllers/API/queries/auth";
import CustomLoader from "@/customization/components/custom-loader";
import useAlertStore from "@/stores/alertStore";
import type { APIObjectType, ComponentVisibility, Users } from "@/types/api";
import { componentVisibilityKey } from "@/utils/component-visibility";
import { SIDEBAR_BUNDLES, SIDEBAR_CATEGORIES } from "@/utils/styleUtils";
import BaseModal from "../baseModal";

type VisibilityDraft = Pick<
  ComponentVisibility,
  "hidden_bundles" | "hidden_components"
>;

const EMPTY_VISIBILITY: VisibilityDraft = {
  hidden_bundles: [],
  hidden_components: [],
};

const bundleNames = new Map(
  [...SIDEBAR_CATEGORIES, ...SIDEBAR_BUNDLES].map((bundle) => [
    bundle.name,
    bundle.display_name,
  ]),
);

interface ComponentVisibilityModalProps {
  open: boolean;
  setOpen: (open: boolean) => void;
  user: Users | null;
  catalog: APIObjectType;
  onSaved: () => void;
}

export default function ComponentVisibilityModal({
  open,
  setOpen,
  user,
  catalog,
  onSaved,
}: ComponentVisibilityModalProps) {
  const { t } = useTranslation();
  const setSuccessData = useAlertStore((state) => state.setSuccessData);
  const setErrorData = useAlertStore((state) => state.setErrorData);
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState<VisibilityDraft>(EMPTY_VISIBILITY);
  const { data: savedVisibility, isLoading } = useGetUserComponentVisibility(
    user?.id,
    { enabled: open && Boolean(user) },
  );
  const { mutate: updateVisibility, isPending } =
    useUpdateComponentVisibility();

  const configurableCatalog = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(catalog).filter(
          ([bundle]) => bundle !== "saved_components" && bundle !== "MCP",
        ),
      ),
    [catalog],
  );

  useEffect(() => {
    if (!open) return;
    setSearch("");
    setDraft(
      savedVisibility
        ? {
            hidden_bundles: savedVisibility.hidden_bundles,
            hidden_components: savedVisibility.hidden_components,
          }
        : EMPTY_VISIBILITY,
    );
  }, [open, savedVisibility]);

  const visibleCatalog = useMemo(() => {
    const normalizedSearch = search.trim().toLocaleLowerCase();
    return Object.entries(configurableCatalog)
      .map(([bundle, components]) => {
        const rawBundleName = bundleNames.get(bundle) ?? bundle;
        const displayBundleName = rawBundleName.startsWith("sidebar.")
          ? t(rawBundleName)
          : rawBundleName;
        const bundleMatches = displayBundleName
          .toLocaleLowerCase()
          .includes(normalizedSearch);
        const matchingComponents = Object.entries(components).filter(
          ([component, data]) =>
            !normalizedSearch ||
            bundleMatches ||
            component.toLocaleLowerCase().includes(normalizedSearch) ||
            data.display_name.toLocaleLowerCase().includes(normalizedSearch),
        );
        return [bundle, displayBundleName, matchingComponents] as const;
      })
      .filter(([, , components]) => components.length > 0)
      .toSorted(([, nameA], [, nameB]) => nameA.localeCompare(nameB));
  }, [configurableCatalog, search, t]);

  const toggleBundle = (bundle: string, visible: boolean) => {
    setDraft((current) => ({
      ...current,
      hidden_bundles: visible
        ? current.hidden_bundles.filter((item) => item !== bundle)
        : Array.from(new Set([...current.hidden_bundles, bundle])),
    }));
  };

  const toggleComponent = (
    bundle: string,
    component: string,
    visible: boolean,
  ) => {
    const key = componentVisibilityKey(bundle, component);
    setDraft((current) => ({
      ...current,
      hidden_components: visible
        ? current.hidden_components.filter((item) => item !== key)
        : Array.from(new Set([...current.hidden_components, key])),
    }));
  };

  const save = () => {
    if (!user) return;
    updateVisibility(
      { userId: user.id, visibility: draft },
      {
        onSuccess: () => {
          setOpen(false);
          onSaved();
          setSuccessData({ title: t("admin.componentVisibilitySaved") });
        },
        onError: () => {
          setErrorData({ title: t("admin.componentVisibilityError") });
        },
      },
    );
  };

  return (
    <BaseModal open={open} setOpen={setOpen} size="large-h-full">
      <BaseModal.Header
        description={t("admin.componentVisibilityDescription", {
          username: user?.username,
        })}
      >
        {t("admin.componentVisibilityTitle")}
      </BaseModal.Header>
      <BaseModal.Content className="gap-4" overflowHidden>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t("admin.componentVisibilitySearch")}
            className="min-w-64 flex-1"
          />
          <Button
            variant="outline"
            size="sm"
            onClick={() => setDraft(EMPTY_VISIBILITY)}
          >
            {t("admin.componentVisibilityAllVisible")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              setDraft((current) => ({
                ...current,
                hidden_bundles: Object.keys(configurableCatalog),
              }))
            }
          >
            {t("admin.componentVisibilityAllHidden")}
          </Button>
        </div>

        {isLoading ? (
          <div className="flex h-full items-center justify-center">
            <CustomLoader remSize={8} />
          </div>
        ) : (
          <div className="custom-scroll flex-1 space-y-3 overflow-y-auto pr-2">
            {visibleCatalog.map(([bundle, bundleName, components]) => {
              const bundleVisible = !draft.hidden_bundles.includes(bundle);
              return (
                <section key={bundle} className="rounded-md border">
                  <label className="flex cursor-pointer items-center gap-3 bg-muted px-4 py-3 font-medium">
                    <Checkbox
                      checked={bundleVisible}
                      onCheckedChange={(checked) =>
                        toggleBundle(bundle, checked === true)
                      }
                    />
                    <span>{bundleName}</span>
                    <span className="ml-auto text-xs text-muted-foreground">
                      {components.length}
                    </span>
                  </label>
                  <div className="grid grid-cols-1 gap-1 p-2 md:grid-cols-2">
                    {components.map(([component, data]) => {
                      const componentVisible =
                        bundleVisible &&
                        !draft.hidden_components.includes(
                          componentVisibilityKey(bundle, component),
                        );
                      return (
                        <label
                          key={component}
                          className="flex cursor-pointer items-center gap-3 rounded px-2 py-2 hover:bg-muted"
                        >
                          <Checkbox
                            checked={componentVisible}
                            disabled={!bundleVisible}
                            onCheckedChange={(checked) =>
                              toggleComponent(
                                bundle,
                                component,
                                checked === true,
                              )
                            }
                          />
                          <span className="min-w-0 truncate text-sm">
                            {data.display_name}
                          </span>
                        </label>
                      );
                    })}
                  </div>
                </section>
              );
            })}
          </div>
        )}
      </BaseModal.Content>
      <BaseModal.Footer
        submit={{
          label: t("admin.saveButton"),
          onClick: save,
          loading: isPending,
          disabled: isLoading || !user,
        }}
      >
        <Button
          variant="ghost"
          type="button"
          onClick={() => setDraft(EMPTY_VISIBILITY)}
        >
          {t("admin.componentVisibilityReset")}
        </Button>
      </BaseModal.Footer>
    </BaseModal>
  );
}
