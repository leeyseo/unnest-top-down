import { type Dispatch, ReactNode, type SetStateAction, useState } from "react";
import { useTranslation } from "react-i18next";
import IconComponent from "@/components/common/genericIconComponent";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { CustomLink } from "@/customization/components/custom-link";
import { ENABLE_WIDGET } from "@/customization/feature-flags";
import { customMcpOpen } from "@/customization/utils/custom-mcp-open";
import ApiModal from "@/modals/apiModal";
import EmbedModal from "@/modals/EmbedModal/embed-modal";
import ExportModal from "@/modals/exportModal";
import useAuthStore from "@/stores/authStore";
import useFlowsManagerStore from "@/stores/flowsManagerStore";
import { cn } from "@/utils/utils";

type PublishDropdownProps = {
  openApiModal: boolean;
  setOpenApiModal: Dispatch<SetStateAction<boolean>>;
  children?: ReactNode;
};

export default function PublishDropdown({
  openApiModal,
  setOpenApiModal,
  children,
}: PublishDropdownProps) {
  const [openEmbedModal, setOpenEmbedModal] = useState(false);
  const currentFlow = useFlowsManagerStore((state) => state.currentFlow);
  const flowId = currentFlow?.id;
  const flowName = currentFlow?.name;
  const folderId = currentFlow?.folder_id;
  const isAuth = useAuthStore((state) => !!state.autoLogin);
  const [openExportModal, setOpenExportModal] = useState(false);
  const { t } = useTranslation();

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="md"
            className="!px-2.5 font-normal"
            data-testid="publish-button"
          >
            {t("misc.share")}
            <IconComponent name="ChevronDown" className="!h-5 !w-5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          forceMount
          sideOffset={7}
          alignOffset={-2}
          align="end"
          className="w-full min-w-[275px]"
        >
          <DropdownMenuItem
            className="deploy-dropdown-item group"
            onClick={() => setOpenApiModal(true)}
            data-testid="api-access-item"
          >
            <IconComponent name="Code2" className={`icon-size mr-2`} />
            <span>{t("misc.apiAccess")}</span>
          </DropdownMenuItem>
          <DropdownMenuItem
            className="deploy-dropdown-item group"
            onClick={() => setOpenExportModal(true)}
          >
            <IconComponent name="Download" className={`icon-size mr-2`} />
            <span>{t("misc.export")}</span>
          </DropdownMenuItem>
          <CustomLink
            className={cn("flex-1")}
            to={`/mcp/folder/${folderId}`}
            target={customMcpOpen()}
          >
            <DropdownMenuItem
              className="deploy-dropdown-item group"
              onClick={() => {}}
              data-testid="mcp-server-item"
            >
              <IconComponent name="Mcp" className={`icon-size mr-2`} />
              <span>{t("misc.mcpServer")}</span>
              <IconComponent
                name="ExternalLink"
                className={`icon-size ml-auto hidden group-hover:block`}
              />
            </DropdownMenuItem>
          </CustomLink>
          {ENABLE_WIDGET && (
            <DropdownMenuItem
              onClick={() => setOpenEmbedModal(true)}
              className="deploy-dropdown-item group"
            >
              <IconComponent name="Columns2" className={`icon-size mr-2`} />
              <span>{t("misc.embedIntoSite")}</span>
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      <ApiModal open={openApiModal} setOpen={setOpenApiModal}>
        <>{children}</>
      </ApiModal>
      <EmbedModal
        open={openEmbedModal}
        setOpen={setOpenEmbedModal}
        flowId={flowId ?? ""}
        flowName={flowName ?? ""}
        isAuth={isAuth}
        tweaksBuildedObject={{}}
        activeTweaks={false}
      ></EmbedModal>
      <ExportModal open={openExportModal} setOpen={setOpenExportModal} />
    </>
  );
}
