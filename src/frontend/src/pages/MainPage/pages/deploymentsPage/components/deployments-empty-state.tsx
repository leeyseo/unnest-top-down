import { useTranslation } from "react-i18next";
import ForwardedIconComponent from "@/components/common/genericIconComponent";
import UnnestEmptyState from "@/components/common/unnest-empty-state";
import { Button } from "@/components/ui/button";

interface DeploymentsEmptyStateProps {
  onAction: () => void;
}

export default function DeploymentsEmptyState({
  onAction,
}: DeploymentsEmptyStateProps) {
  const { t } = useTranslation();
  return (
    <UnnestEmptyState
      image="Birds/03-eagle.svg"
      title={t("deployments.noDeployments")}
      description={t("deployments.emptyStateDescription")}
      className="py-16"
    >
      <Button
        variant="outline"
        data-testid="create-deployment-empty-btn"
        onClick={onAction}
      >
        <ForwardedIconComponent name="Plus" className="h-4 w-4" />
        {t("deployments.createDeployment")}
      </Button>
    </UnnestEmptyState>
  );
}
