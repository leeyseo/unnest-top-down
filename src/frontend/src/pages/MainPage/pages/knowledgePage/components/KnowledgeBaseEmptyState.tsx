import { useState } from "react";
import { useTranslation } from "react-i18next";
import ForwardedIconComponent from "@/components/common/genericIconComponent";
import UnnestEmptyState from "@/components/common/unnest-empty-state";
import { Button } from "@/components/ui/button";
import KnowledgeBaseUploadModal from "@/modals/knowledgeBaseUploadModal/KnowledgeBaseUploadModal";
import useAlertStore from "@/stores/alertStore";
import { useOptimisticKnowledgeBase } from "../hooks/useOptimisticKnowledgeBase";

const KnowledgeBaseEmptyState = ({
  handleCreateKnowledge,
}: {
  handleCreateKnowledge: () => void;
}) => {
  const { t } = useTranslation();
  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);
  const setSuccessData = useAlertStore((state) => state.setSuccessData);
  const { captureSubmit, applyOptimisticUpdate } = useOptimisticKnowledgeBase();

  return (
    <>
      <UnnestEmptyState
        image="Space/02-eggs.png"
        title={t("knowledge.noKnowledgeBases")}
        description={t("knowledge.emptyDescription")}
      >
        <Button
          className="flex items-center gap-2 font-semibold"
          onClick={() => setIsUploadModalOpen(true)}
        >
          <ForwardedIconComponent name="Plus" className="h-4 w-4" />
          {t("knowledge.addKnowledge")}
        </Button>
      </UnnestEmptyState>

      <KnowledgeBaseUploadModal
        open={isUploadModalOpen}
        setOpen={(open) => {
          setIsUploadModalOpen(open);
          if (!open) {
            applyOptimisticUpdate();
          }
        }}
        onSubmit={(data) => {
          captureSubmit(data);
          setSuccessData({
            title: t("knowledge.baseCreated", { name: data.sourceName }),
          });
        }}
      />
    </>
  );
};

export default KnowledgeBaseEmptyState;
