import { useTranslation } from "react-i18next";
import ForwardedIconComponent from "@/components/common/genericIconComponent";
import UnnestEmptyState from "@/components/common/unnest-empty-state";
import { Button } from "@/components/ui/button";
import { useFolderStore } from "@/stores/foldersStore";
import { useUtilityStore } from "@/stores/utilityStore";

type EmptyFolderProps = {
  setOpenModal: (open: boolean) => void;
  /** Preferred handler — bypasses the templates modal and starts a fresh
   *  flow with the welcome overlay primed. Falls back to ``setOpenModal``
   *  when omitted to keep legacy callers working. */
  onNewFlow?: () => void;
};

export const EmptyFolder = ({ setOpenModal, onNewFlow }: EmptyFolderProps) => {
  const { t } = useTranslation();
  const folders = useFolderStore((state) => state.folders);
  const hideNewFlowButton = useUtilityStore((state) => state.hideNewFlowButton);

  return (
    <UnnestEmptyState
      image="Space/01-nest.png"
      title={
        folders?.length > 1
          ? t("emptyPage.emptyProject")
          : t("emptyPage.startBuilding")
      }
      description={t("emptyPage.description")}
      titleTestId="mainpage_title"
      descriptionTestId="empty-project-description"
    >
      {!hideNewFlowButton && (
        <Button
          variant="default"
          onClick={() => (onNewFlow ? onNewFlow() : setOpenModal(true))}
          id="new-project-btn"
          data-testid="new_project_btn_empty_page"
        >
          <ForwardedIconComponent
            name="Plus"
            aria-hidden="true"
            className="h-4 w-4"
          />
          <span className="whitespace-nowrap font-semibold">
            {t("emptyPage.newFlow")}
          </span>
        </Button>
      )}
    </UnnestEmptyState>
  );
};

export default EmptyFolder;
