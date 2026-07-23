import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useGetFlowVersions } from "@/controllers/API/queries/flow-version/use-get-flow-versions";
import { useGetRefreshFlowsQuery } from "@/controllers/API/queries/flows/use-get-refresh-flows-query";
import {
  useExportOnPremRelease,
  useValidateOnPremRelease,
} from "@/controllers/API/queries/on-prem-deployments/use-on-prem-release";
import { useFolderStore } from "@/stores/foldersStore";
import type { FlowType, PaginatedFlowsType } from "@/types/flow";
import {
  buildOnPremReleasePayload,
  defaultOnPremWizardValues,
  type OnPremWizardValues,
} from "../helpers/on-prem-release";
import { useErrorAlert } from "../hooks/use-error-alert";

const steps = ["Flows & API", "Infrastructure", "Operations", "Review"];

type SelectOption = { label: string; value: string };

function Field({
  id,
  label,
  value,
  onChange,
  type = "text",
  min,
}: {
  id: string;
  label: string;
  value: string | number;
  onChange: (value: string) => void;
  type?: "text" | "number" | "color";
  min?: number;
}) {
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type={type}
        min={min}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

function SelectField({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string;
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      <Select value={value} onValueChange={onChange} disabled={disabled}>
        <SelectTrigger className="w-full">
          <SelectValue placeholder="Select…" />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function ToggleField({
  id,
  label,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-4 rounded-md border p-3">
      <Label htmlFor={id}>{label}</Label>
      <Switch id={id} checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

function flowOptions(flows: FlowType[]) {
  return flows.map((flow) => ({ label: flow.name, value: flow.id }));
}

function versionOptions(
  versions:
    | { id: string; version_number: number; version_tag: string }[]
    | undefined,
) {
  return (versions ?? []).map((version) => ({
    label: version.version_tag || `v${version.version_number}`,
    value: version.id,
  }));
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
  const [jsonError, setJsonError] = useState("");
  const showError = useErrorAlert();

  const flowsQuery = useGetRefreshFlowsQuery(
    {
      folder_id: folderId ?? myCollectionId ?? undefined,
      get_all: true,
      remove_example_flows: true,
    },
    { enabled: open },
  );
  const flowsData = flowsQuery.data as
    | FlowType[]
    | PaginatedFlowsType
    | undefined;
  const flows = Array.isArray(flowsData) ? flowsData : (flowsData?.items ?? []);

  const [agentFlowId, setAgentFlowId] = useState("");
  const [ingestionFlowId, setIngestionFlowId] = useState("");
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
  const flowChoices = useMemo(() => flowOptions(flows), [flows]);

  const update = <K extends keyof OnPremWizardValues>(
    key: K,
    value: OnPremWizardValues[K],
  ) => {
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

  const validate = async () => {
    const request = payload();
    if (!request) return;
    try {
      await validation.mutateAsync(request);
    } catch (error) {
      showError("Could not validate on-prem release", error);
    }
  };

  const submit = async () => {
    const request = payload();
    if (!request) return;
    try {
      await exportRelease.mutateAsync(request);
    } catch (error) {
      showError("Could not export on-prem release", error);
    }
  };

  const resetAndClose = () => {
    setStep(0);
    setValues(defaultOnPremWizardValues);
    setAgentFlowId("");
    setIngestionFlowId("");
    setJsonError("");
    validation.reset();
    exportRelease.reset();
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

  return (
    <Dialog open={open} onOpenChange={(value) => !value && resetAndClose()}>
      <DialogContent className="h-[88vh] w-[960px] max-w-[95vw]">
        <DialogHeader>
          <DialogTitle>Export Unnest on-prem release</DialogTitle>
          <DialogDescription>
            Package immutable Agent and Ingestion Flow versions for an offline
            installation.
          </DialogDescription>
        </DialogHeader>

        <ol className="grid grid-cols-4 gap-2" aria-label="Export progress">
          {steps.map((name, index) => (
            <li
              key={name}
              aria-current={index === step ? "step" : undefined}
              className={`rounded-md border px-3 py-2 text-sm ${
                index === step ? "border-primary bg-muted font-medium" : ""
              }`}
            >
              {index + 1}. {name}
            </li>
          ))}
        </ol>

        <div className="min-h-0 flex-1 overflow-y-auto pr-2">
          {step === 0 && (
            <div className="space-y-5">
              <div className="grid grid-cols-2 gap-4">
                <Field
                  id="on-prem-release-version"
                  label="Release version (SemVer)"
                  value={values.releaseVersion}
                  onChange={(value) => update("releaseVersion", value)}
                />
                <div />
                <SelectField
                  label="Agent Flow"
                  value={agentFlowId}
                  options={flowChoices}
                  onChange={(value) => {
                    setAgentFlowId(value);
                    update("agentFlowVersionId", "");
                  }}
                />
                <SelectField
                  label="Saved Agent Flow Version"
                  value={values.agentFlowVersionId}
                  options={versionOptions(agentVersionQuery.data?.entries)}
                  onChange={(value) => update("agentFlowVersionId", value)}
                  disabled={!agentFlowId}
                />
                <SelectField
                  label="Ingestion Flow"
                  value={ingestionFlowId}
                  options={flowChoices}
                  onChange={(value) => {
                    setIngestionFlowId(value);
                    update("ingestionFlowVersionId", "");
                  }}
                />
                <SelectField
                  label="Saved Ingestion Flow Version"
                  value={values.ingestionFlowVersionId}
                  options={versionOptions(ingestionVersionQuery.data?.entries)}
                  onChange={(value) => update("ingestionFlowVersionId", value)}
                  disabled={!ingestionFlowId}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="on-prem-api-contract">
                  API contract (JSON Schema, examples and component mappings)
                </Label>
                <p className="text-xs text-muted-foreground">
                  Replace each empty component_id with a node ID from the saved
                  Agent Flow. Required schema fields must have mappings.
                </p>
                <Textarea
                  id="on-prem-api-contract"
                  className="min-h-72 font-mono text-xs"
                  value={values.apiContract}
                  onChange={(event) =>
                    update("apiContract", event.target.value)
                  }
                />
                {jsonError && (
                  <p className="text-sm text-destructive">{jsonError}</p>
                )}
              </div>
            </div>
          )}

          {step === 1 && (
            <div className="grid grid-cols-2 gap-4">
              <SelectField
                label="Package"
                value={values.orchestrator}
                options={[
                  { value: "compose", label: "Docker / Podman Compose" },
                  { value: "helm", label: "Kubernetes / Helm" },
                ]}
                onChange={(value) =>
                  update("orchestrator", value as "compose" | "helm")
                }
                disabled={values.topology === "ha"}
              />
              <SelectField
                label="Topology"
                value={values.topology}
                options={[
                  { value: "single", label: "Single server" },
                  { value: "ha", label: "Kubernetes HA" },
                ]}
                onChange={(value) => {
                  update("topology", value as "single" | "ha");
                  if (value === "ha") update("orchestrator", "helm");
                }}
              />
              <SelectField
                label="Architecture"
                value={values.architecture}
                options={[
                  { value: "amd64", label: "AMD64" },
                  { value: "arm64", label: "ARM64" },
                ]}
                onChange={(value) =>
                  update("architecture", value as "amd64" | "arm64")
                }
              />
              <SelectField
                label="Accelerator"
                value={values.accelerator}
                options={[
                  { value: "cpu", label: "CPU" },
                  { value: "nvidia", label: "NVIDIA GPU" },
                  { value: "amd", label: "AMD ROCm" },
                ]}
                onChange={(value) =>
                  update("accelerator", value as "cpu" | "nvidia" | "amd")
                }
              />
              <SelectField
                label="PostgreSQL"
                value={values.database}
                options={[
                  { value: "embedded-postgresql", label: "Bundled" },
                  { value: "external-postgresql", label: "Institution" },
                ]}
                onChange={(value) =>
                  update(
                    "database",
                    value as "embedded-postgresql" | "external-postgresql",
                  )
                }
              />
              <SelectField
                label="Storage"
                value={values.storage}
                options={[
                  { value: "local", label: "Local volume" },
                  { value: "minio", label: "MinIO" },
                  { value: "s3", label: "S3 compatible" },
                ]}
                onChange={(value) =>
                  update("storage", value as "local" | "minio" | "s3")
                }
              />
              <SelectField
                label="Infrastructure"
                value={values.infrastructure}
                options={[
                  { value: "bundled", label: "Bundled services" },
                  { value: "external", label: "Institution services" },
                ]}
                onChange={(value) =>
                  update("infrastructure", value as "bundled" | "external")
                }
              />
              <SelectField
                label="Model runtime"
                value={values.modelRuntime}
                options={[
                  { value: "external", label: "External endpoint" },
                  { value: "bundled", label: "Bundled local model" },
                ]}
                onChange={(value) =>
                  update("modelRuntime", value as "external" | "bundled")
                }
              />
              <Field
                id="on-prem-cpu"
                label="CPU cores"
                type="number"
                min={1}
                value={values.cpu}
                onChange={(value) => update("cpu", Number(value))}
              />
              <Field
                id="on-prem-memory"
                label="Memory (GiB)"
                type="number"
                min={1}
                value={values.memoryGiB}
                onChange={(value) => update("memoryGiB", Number(value))}
              />
              <Field
                id="on-prem-disk"
                label="Disk (GiB)"
                type="number"
                min={1}
                value={values.diskGiB}
                onChange={(value) => update("diskGiB", Number(value))}
              />
              {values.accelerator !== "cpu" && (
                <Field
                  id="on-prem-gpu"
                  label="GPU count"
                  type="number"
                  min={1}
                  value={values.gpuCount}
                  onChange={(value) => update("gpuCount", Number(value))}
                />
              )}
              <ToggleField
                id="on-prem-model-weights"
                label="Include pinned model weights"
                checked={values.includeModelWeights}
                onChange={(value) => update("includeModelWeights", value)}
              />
            </div>
          )}

          {step === 2 && (
            <div className="space-y-6">
              <div className="grid grid-cols-2 gap-4">
                <SelectField
                  label="TLS certificate"
                  value={values.tls}
                  options={[
                    { value: "institution", label: "Institution certificate" },
                    { value: "self-signed", label: "Self-signed (test only)" },
                  ]}
                  onChange={(value) =>
                    update("tls", value as "institution" | "self-signed")
                  }
                />
                <SelectField
                  label="Default language"
                  value={values.defaultLanguage}
                  options={[
                    { value: "ko", label: "한국어" },
                    { value: "en", label: "English" },
                  ]}
                  onChange={(value) =>
                    update("defaultLanguage", value as "ko" | "en")
                  }
                />
                <Field
                  id="on-prem-solution"
                  label="Solution name"
                  value={values.solutionName}
                  onChange={(value) => update("solutionName", value)}
                />
                <Field
                  id="on-prem-organization"
                  label="Organization"
                  value={values.organizationName}
                  onChange={(value) => update("organizationName", value)}
                />
                <Field
                  id="on-prem-primary-color"
                  label="Primary color"
                  type="color"
                  value={values.primaryColor}
                  onChange={(value) => update("primaryColor", value)}
                />
                <Field
                  id="on-prem-login-notice"
                  label="Login notice"
                  value={values.loginNotice}
                  onChange={(value) => update("loginNotice", value)}
                />
                <Field
                  id="on-prem-retention"
                  label="Artifact retention (days)"
                  type="number"
                  min={1}
                  value={values.retentionDays}
                  onChange={(value) => update("retentionDays", Number(value))}
                />
                <Field
                  id="on-prem-retention-size"
                  label="Artifact limit (GiB)"
                  type="number"
                  min={1}
                  value={values.retentionMaxGiB}
                  onChange={(value) => update("retentionMaxGiB", Number(value))}
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                {(
                  [
                    ["signing", "Sign artifacts"],
                    ["backups", "Include backup support"],
                    ["monitoring", "Include monitoring"],
                    ["clamav", "Include ClamAV"],
                    ["grafana", "Include Grafana"],
                    ["pinned", "Pin this release"],
                    ["allowLanguageSwitch", "Allow language switching"],
                    ["showUnnestBranding", "Show Unnest branding"],
                    ["storeConversations", "Store conversations"],
                  ] as const
                ).map(([key, label]) => (
                  <ToggleField
                    key={key}
                    id={`on-prem-${key}`}
                    label={label}
                    checked={values[key]}
                    onChange={(value) => update(key, value)}
                  />
                ))}
              </div>
              {values.storeConversations && (
                <Field
                  id="on-prem-conversation-retention"
                  label="Conversation retention (days)"
                  type="number"
                  min={1}
                  value={values.conversationRetentionDays}
                  onChange={(value) =>
                    update("conversationRetentionDays", Number(value))
                  }
                />
              )}
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="on-prem-endpoints">
                    External endpoints (one per line)
                  </Label>
                  <Textarea
                    id="on-prem-endpoints"
                    className="min-h-24"
                    value={values.externalEndpoints}
                    onChange={(event) =>
                      update("externalEndpoints", event.target.value)
                    }
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="on-prem-secrets">
                    Required secret names (one per line)
                  </Label>
                  <Textarea
                    id="on-prem-secrets"
                    className="min-h-24"
                    value={values.secretNames}
                    onChange={(event) =>
                      update("secretNames", event.target.value)
                    }
                  />
                </div>
              </div>
            </div>
          )}

          {step === 3 && (
            <div className="space-y-5">
              <div className="grid grid-cols-2 gap-3 rounded-md border p-4 text-sm">
                <span className="text-muted-foreground">Release</span>
                <span>{values.releaseVersion}</span>
                <span className="text-muted-foreground">Target</span>
                <span>
                  {values.architecture} · {values.orchestrator} ·{" "}
                  {values.topology}
                </span>
                <span className="text-muted-foreground">Resources</span>
                <span>
                  {values.cpu} CPU · {values.memoryGiB} GiB RAM ·{" "}
                  {values.diskGiB} GiB disk
                </span>
                <span className="text-muted-foreground">Data</span>
                <span>
                  {values.database} · {values.storage}
                </span>
                <span className="text-muted-foreground">Security</span>
                <span>
                  {values.signing ? "signed" : "checksums only"} · {values.tls}
                </span>
                <span className="text-muted-foreground">Brand</span>
                <span>
                  {values.organizationName || "—"} / {values.solutionName}
                </span>
              </div>

              {!validation.data && (
                <p className="text-sm text-muted-foreground">
                  Run static validation before creating the immutable release.
                </p>
              )}
              {validation.data?.errors.map((error) => (
                <p key={error} className="text-sm text-destructive">
                  {error}
                </p>
              ))}
              {validation.data?.warnings.map((warning) => (
                <p key={warning} className="text-sm text-muted-foreground">
                  {warning}
                </p>
              ))}
              {validated && (
                <p className="text-sm text-primary">
                  Validation passed. API version:{" "}
                  {String(
                    (
                      validation.data?.manifest?.api as
                        | Record<string, unknown>
                        | undefined
                    )?.version ?? "v1",
                  )}
                </p>
              )}
              {exportRelease.data && (
                <div className="rounded-md border border-primary p-4 text-sm">
                  Release {exportRelease.data.release.release_version} was
                  created. Build status: {exportRelease.data.build.status}.
                </div>
              )}
            </div>
          )}
        </div>

        <DialogFooter className="border-t pt-4">
          {exportRelease.data ? (
            <Button onClick={resetAndClose}>Done</Button>
          ) : (
            <>
              <Button
                variant="outline"
                onClick={() =>
                  step === 0 ? resetAndClose() : setStep(step - 1)
                }
                disabled={validation.isPending || exportRelease.isPending}
              >
                {step === 0 ? "Cancel" : "Back"}
              </Button>
              {step < 3 ? (
                <Button
                  onClick={() => {
                    if (step === 0 && !payload()) return;
                    setStep(step + 1);
                  }}
                  disabled={!canContinue}
                >
                  Next
                </Button>
              ) : (
                <>
                  <Button
                    variant="outline"
                    onClick={validate}
                    disabled={validation.isPending || exportRelease.isPending}
                  >
                    {validation.isPending ? "Validating…" : "Validate"}
                  </Button>
                  <Button
                    onClick={submit}
                    disabled={!validated || exportRelease.isPending}
                  >
                    {exportRelease.isPending
                      ? "Starting build…"
                      : "Create release & build"}
                  </Button>
                </>
              )}
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
