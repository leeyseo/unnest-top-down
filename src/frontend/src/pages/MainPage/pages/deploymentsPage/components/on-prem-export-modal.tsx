import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useGetFilesV2 } from "@/controllers/API/queries/file-management";
import { useGetFlowVersions } from "@/controllers/API/queries/flow-version/use-get-flow-versions";
import { useGetRefreshFlowsQuery } from "@/controllers/API/queries/flows/use-get-refresh-flows-query";
import {
  useExportOnPremRelease,
  usePushOnPremRegistry,
  useSyncOnPremBuild,
  useValidateOnPremRelease,
} from "@/controllers/API/queries/on-prem-deployments/use-on-prem-release";
import { useFolderStore } from "@/stores/foldersStore";
import { useUtilityStore } from "@/stores/utilityStore";
import type { FlowType, PaginatedFlowsType } from "@/types/flow";
import { cn } from "@/utils/utils";
import {
  buildOnPremReleasePayload,
  defaultOnPremWizardValues,
  type OnPremWizardUpdate,
  type OnPremWizardValues,
} from "../helpers/on-prem-release";
import { useErrorAlert } from "../hooks/use-error-alert";
import { OnPremExportFooter } from "./on-prem-export-footer";
import { OnPremFlowsApiStep } from "./on-prem-flows-api-step";
import { OnPremInfrastructureStep } from "./on-prem-infrastructure-step";
import { OnPremOperationsStep } from "./on-prem-operations-step";
import { OnPremReviewStep } from "./on-prem-review-step";

const STEPS = ["Flows & API", "Infrastructure", "Operations", "Review"];

function flowOptions(flows: FlowType[]) {
  return flows.map((flow) => ({ label: flow.name, value: flow.id }));
}

export default function OnPremExportModal({
  open,
  setOpen,
}: {
  open: boolean;
  setOpen: (open: boolean) => void;
}) {
  const { folderId } = useParams();
  const myCollectionId = useFolderStore((state) => state.myCollectionId);
  const [step, setStep] = useState(0);
  const [values, setValues] = useState<OnPremWizardValues>(
    defaultOnPremWizardValues,
  );
  const [agentFlowId, setAgentFlowId] = useState("");
  const [ingestionFlowId, setIngestionFlowId] = useState("");
  const [jsonError, setJsonError] = useState("");
  const [registryReference, setRegistryReference] = useState("");
  const [registrySecretName, setRegistrySecretName] = useState("");
  const buildsEnabled = useUtilityStore(
    (state) => state.featureFlags.on_prem_builds === true,
  );
  const showError = useErrorAlert();

  const flowsQuery = useGetRefreshFlowsQuery(
    {
      folder_id: folderId ?? myCollectionId ?? undefined,
      get_all: true,
      remove_example_flows: true,
    },
    { enabled: open },
  );
  const filesQuery = useGetFilesV2({ enabled: open });
  const agentVersionQuery = useGetFlowVersions(
    { flowId: agentFlowId },
    { enabled: open && !!agentFlowId },
  );
  const ingestionVersionQuery = useGetFlowVersions(
    { flowId: ingestionFlowId },
    { enabled: open && !!ingestionFlowId },
  );
  const validation = useValidateOnPremRelease();
  const exportRelease = useExportOnPremRelease();
  const syncBuild = useSyncOnPremBuild();
  const pushRegistry = usePushOnPremRegistry();

  const flowsData = flowsQuery.data as
    | FlowType[]
    | PaginatedFlowsType
    | undefined;
  const flows = Array.isArray(flowsData) ? flowsData : (flowsData?.items ?? []);
  const flowChoices = useMemo(() => flowOptions(flows), [flows]);

  const update: OnPremWizardUpdate = (key, value) => {
    setValues((current) => ({ ...current, [key]: value }));
    validation.reset();
  };

  const payload = () => {
    try {
      const result = buildOnPremReleasePayload(values);
      setJsonError("");
      return result;
    } catch {
      setJsonError("API contract must be valid JSON.");
      return null;
    }
  };

  const validate = () => {
    const request = payload();
    if (!request) return;
    validation.mutate(request, {
      onError: (error) =>
        showError("Could not validate on-prem release", error),
    });
  };

  const submit = () => {
    const request = payload();
    if (!request) return;
    exportRelease.mutate(request, {
      onError: (error) => showError("Could not export on-prem release", error),
    });
  };

  const refreshBuild = () => {
    if (!exportRelease.data) return;
    syncBuild.mutate(
      {
        releaseId: exportRelease.data.release.id,
        buildId: exportRelease.data.build.id,
      },
      {
        onError: (error) => showError("Could not refresh on-prem build", error),
      },
    );
  };

  const pushToRegistry = () => {
    if (!exportRelease.data) return;
    pushRegistry.mutate(
      {
        releaseId: exportRelease.data.release.id,
        buildId: exportRelease.data.build.id,
        reference: registryReference.trim(),
        credentialSecretName: registrySecretName.trim(),
      },
      {
        onError: (error) =>
          showError("Could not push build to registry", error),
      },
    );
  };

  const resetAndClose = () => {
    setStep(0);
    setValues(defaultOnPremWizardValues);
    setAgentFlowId("");
    setIngestionFlowId("");
    setJsonError("");
    setRegistryReference("");
    setRegistrySecretName("");
    validation.reset();
    exportRelease.reset();
    syncBuild.reset();
    pushRegistry.reset();
    setOpen(false);
  };

  const canContinue =
    step !== 0 ||
    (!!values.releaseVersion &&
      !!values.agentFlowVersionId &&
      !!values.ingestionFlowVersionId &&
      values.agentFlowVersionId !== values.ingestionFlowVersionId);
  const validated =
    validation.data != null && validation.data.errors.length === 0;
  const currentBuild = syncBuild.data ?? exportRelease.data?.build;
  const downloadableArtifact = currentBuild?.artifacts?.find((artifact) =>
    ["tar", "package"].includes(artifact.artifact_type),
  );

  return (
    <Dialog open={open} onOpenChange={(value) => !value && resetAndClose()}>
      <DialogContent className="h-[88vh] w-[960px] max-w-[95vw]">
        <DialogHeader>
          <DialogTitle>Export Unnest on-prem release</DialogTitle>
          <DialogDescription>
            Package immutable Flows and source documents for an offline
            installation.
          </DialogDescription>
        </DialogHeader>

        <ol className="grid grid-cols-4 gap-2" aria-label="Export progress">
          {STEPS.map((name, index) => (
            <li
              key={name}
              aria-current={index === step ? "step" : undefined}
              className={cn(
                "rounded-md border px-3 py-2 text-sm",
                index === step && "border-primary bg-muted font-medium",
              )}
            >
              {index + 1}. {name}
            </li>
          ))}
        </ol>

        <div className="min-h-0 flex-1 overflow-y-auto pr-2">
          {step === 0 && (
            <OnPremFlowsApiStep
              values={values}
              update={update}
              flowChoices={flowChoices}
              agentFlowId={agentFlowId}
              ingestionFlowId={ingestionFlowId}
              onAgentFlowChange={(flowId) => {
                setAgentFlowId(flowId);
                update("agentFlowVersionId", "");
              }}
              onIngestionFlowChange={(flowId) => {
                setIngestionFlowId(flowId);
                update("ingestionFlowVersionId", "");
              }}
              agentVersions={agentVersionQuery.data?.entries}
              ingestionVersions={ingestionVersionQuery.data?.entries}
              files={filesQuery.data ?? []}
              filesLoading={filesQuery.isLoading}
              filesError={filesQuery.isError}
              jsonError={jsonError}
            />
          )}
          {step === 1 && (
            <OnPremInfrastructureStep values={values} update={update} />
          )}
          {step === 2 && (
            <OnPremOperationsStep values={values} update={update} />
          )}
          {step === 3 && (
            <OnPremReviewStep
              values={values}
              validation={validation.data}
              validated={validated}
              exportResult={exportRelease.data}
              currentBuild={currentBuild}
              downloadableArtifact={downloadableArtifact}
              registryReference={registryReference}
              registrySecretName={registrySecretName}
              registryArtifact={pushRegistry.data}
              refreshing={syncBuild.isPending}
              pushing={pushRegistry.isPending}
              onRegistryReferenceChange={setRegistryReference}
              onRegistrySecretNameChange={setRegistrySecretName}
              onRefreshBuild={refreshBuild}
              onPushRegistry={pushToRegistry}
            />
          )}
        </div>

        <DialogFooter className="border-t pt-4">
          <div className="flex w-full items-center justify-between gap-4">
            {!buildsEnabled && (
              <p className="text-sm text-muted-foreground" role="status">
                Build server is not configured. Validation is available, but
                release builds are disabled.
              </p>
            )}
            <div className="ml-auto flex gap-2">
              <OnPremExportFooter
                step={step}
                exported={!!exportRelease.data}
                canContinue={canContinue}
                validated={validated}
                buildsEnabled={buildsEnabled}
                validating={validation.isPending}
                exporting={exportRelease.isPending}
                onBack={() =>
                  step === 0 ? resetAndClose() : setStep(step - 1)
                }
                onNext={() => {
                  if (step === 0 && !payload()) return;
                  setStep(step + 1);
                }}
                onValidate={validate}
                onSubmit={submit}
                onDone={resetAndClose}
              />
            </div>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
