import { useTranslation } from "react-i18next";
import { ForwardedIconComponent } from "@/components/common/genericIconComponent";
import UnnestEmptyState from "@/components/common/unnest-empty-state";
import CardsWrapComponent from "@/components/core/cardsWrapComponent";
import { useStartNewFlow } from "@/components/core/flowBuilderWelcome/hooks/use-start-new-flow";
import { Button } from "@/components/ui/button";
import { DotBackgroundDemo } from "@/components/ui/dot-background";
import { useFolderStore } from "@/stores/foldersStore";
import useFileDrop from "../hooks/use-on-file-drop";

export const UnnestWelcomeEmptyState = () => {
  const { t } = useTranslation();
  const handleFileDrop = useFileDrop(undefined);
  const folders = useFolderStore((state) => state.folders);
  const startNewFlow = useStartNewFlow();

  return (
    <DotBackgroundDemo>
      <CardsWrapComponent
        dragMessage={t("home.dragFlowsOrComponents")}
        onFileDrop={handleFileDrop}
      >
        <div className="m-0 h-full w-full bg-background p-0">
          <UnnestEmptyState
            image="Birds/01-owl.svg"
            title={t("page.welcomeTitle")}
            description={
              folders?.length > 1
                ? t("page.emptyFolder")
                : t("page.welcomeDescription")
            }
            titleTestId="mainpage_title"
            descriptionTestId="empty-project-description"
          >
            <Button
              variant="default"
              className="h-auto min-h-10 whitespace-normal rounded-lg font-bold"
              onClick={startNewFlow}
              id="new-project-btn"
              data-testid="new_project_btn_empty_page"
            >
              <ForwardedIconComponent
                name="Plus"
                aria-hidden="true"
                className="h-4 w-4"
              />
              <span>{t("page.createFirstFlow")}</span>
            </Button>
          </UnnestEmptyState>
        </div>
        <p
          data-testid="empty_page_drag_and_drop_text"
          className="absolute bottom-5 left-0 right-0 mt-4 cursor-default text-center text-xxs text-muted-foreground"
        >
          {t("page.dragAndDropText")}
        </p>
      </CardsWrapComponent>
    </DotBackgroundDemo>
  );
};

export default UnnestWelcomeEmptyState;
