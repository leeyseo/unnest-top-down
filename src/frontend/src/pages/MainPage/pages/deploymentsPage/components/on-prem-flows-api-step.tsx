import { useMemo } from "react";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { FileType } from "@/types/file_management";
import type {
  OnPremWizardUpdate,
  OnPremWizardValues,
} from "../helpers/on-prem-release";
import { Field, SelectField } from "./on-prem-export-fields";

type SelectOption = { label: string; value: string };
type Version = { id: string; version_number: number; version_tag: string };
const MAX_SOURCE_DOCUMENTS = 1000;

function versionOptions(versions: Version[] | undefined) {
  return (versions ?? []).map((version) => ({
    label: version.version_tag || `v${version.version_number}`,
    value: version.id,
  }));
}

function formatBytes(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KiB`;
  return `${(size / 1024 ** 2).toFixed(1)} MiB`;
}

export function OnPremFlowsApiStep({
  values,
  update,
  flowChoices,
  agentFlowId,
  ingestionFlowId,
  onAgentFlowChange,
  onIngestionFlowChange,
  agentVersions,
  ingestionVersions,
  files,
  filesLoading,
  filesError,
  jsonError,
}: {
  values: OnPremWizardValues;
  update: OnPremWizardUpdate;
  flowChoices: SelectOption[];
  agentFlowId: string;
  ingestionFlowId: string;
  onAgentFlowChange: (flowId: string) => void;
  onIngestionFlowChange: (flowId: string) => void;
  agentVersions: Version[] | undefined;
  ingestionVersions: Version[] | undefined;
  files: FileType[];
  filesLoading: boolean;
  filesError: boolean;
  jsonError: string;
}) {
  const selected = useMemo(
    () => new Set(values.sourceFileIds),
    [values.sourceFileIds],
  );

  const toggleSource = (fileId: string, checked: boolean) => {
    update(
      "sourceFileIds",
      checked
        ? [...values.sourceFileIds, fileId]
        : values.sourceFileIds.filter((id) => id !== fileId),
    );
  };

  return (
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
          onChange={onAgentFlowChange}
        />
        <SelectField
          label="Saved Agent Flow Version"
          value={values.agentFlowVersionId}
          options={versionOptions(agentVersions)}
          onChange={(value) => update("agentFlowVersionId", value)}
          disabled={!agentFlowId}
        />
        <SelectField
          label="Ingestion Flow"
          value={ingestionFlowId}
          options={flowChoices}
          onChange={onIngestionFlowChange}
        />
        <SelectField
          label="Saved Ingestion Flow Version"
          value={values.ingestionFlowVersionId}
          options={versionOptions(ingestionVersions)}
          onChange={(value) => update("ingestionFlowVersionId", value)}
          disabled={!ingestionFlowId}
        />
      </div>

      <fieldset className="space-y-3 rounded-md border p-4">
        <legend className="px-1 text-sm font-medium">
          Source documents ({values.sourceFileIds.length} selected)
        </legend>
        <p className="text-xs text-muted-foreground">
          Selected SI files are hash-locked into the offline package and copied
          into government-local storage during installation. Government uploads
          are never sent back to the SI through MCP.
        </p>
        {values.sourceFileIds.length >= MAX_SOURCE_DOCUMENTS && (
          <p className="text-sm text-warning-foreground">
            The maximum of {MAX_SOURCE_DOCUMENTS} source documents is selected.
          </p>
        )}
        {filesLoading && (
          <p className="text-sm text-muted-foreground">Loading files…</p>
        )}
        {filesError && (
          <p className="text-sm text-destructive">
            Source files could not be loaded.
          </p>
        )}
        {!filesLoading && !filesError && files.length === 0 && (
          <p className="text-sm text-muted-foreground">
            Upload source files from the Files page before creating the release.
          </p>
        )}
        <div className="max-h-48 space-y-2 overflow-y-auto">
          {files.map((file) => {
            const checkboxId = `on-prem-source-${file.id}`;
            return (
              <div
                key={file.id}
                className="flex items-center gap-3 rounded-md border p-3"
              >
                <Checkbox
                  id={checkboxId}
                  data-testid={checkboxId}
                  checked={selected.has(file.id)}
                  disabled={
                    !selected.has(file.id) &&
                    selected.size >= MAX_SOURCE_DOCUMENTS
                  }
                  onCheckedChange={(checked) =>
                    toggleSource(file.id, checked === true)
                  }
                />
                <Label
                  htmlFor={checkboxId}
                  className="flex min-w-0 flex-1 cursor-pointer justify-between gap-3"
                >
                  <span className="truncate">{file.name}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {formatBytes(file.size)}
                  </span>
                </Label>
              </div>
            );
          })}
        </div>
      </fieldset>

      <div className="space-y-2">
        <Label htmlFor="on-prem-api-contract">
          API contract (JSON Schema, examples and component mappings)
        </Label>
        <p className="text-xs text-muted-foreground">
          Replace each empty component_id with a node ID from the saved Agent
          Flow. Required schema fields must have mappings.
        </p>
        <Textarea
          id="on-prem-api-contract"
          className="min-h-72 font-mono text-xs"
          value={values.apiContract}
          onChange={(event) => update("apiContract", event.target.value)}
        />
        {jsonError && <p className="text-sm text-destructive">{jsonError}</p>}
      </div>
    </div>
  );
}
