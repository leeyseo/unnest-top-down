import { Button } from "@/components/ui/button";
import type {
  OnPremArtifact,
  OnPremBuild,
  OnPremExportResult,
  OnPremReleaseValidation,
} from "@/controllers/API/queries/on-prem-deployments/types";
import { getOnPremArtifactDownloadUrl } from "@/controllers/API/queries/on-prem-deployments/use-on-prem-release";
import type { OnPremWizardValues } from "../helpers/on-prem-release";
import { Field } from "./on-prem-export-fields";

export function OnPremReviewStep({
  values,
  validation,
  validated,
  exportResult,
  currentBuild,
  downloadableArtifact,
  registryReference,
  registrySecretName,
  registryArtifact,
  refreshing,
  pushing,
  onRegistryReferenceChange,
  onRegistrySecretNameChange,
  onRefreshBuild,
  onPushRegistry,
}: {
  values: OnPremWizardValues;
  validation: OnPremReleaseValidation | undefined;
  validated: boolean;
  exportResult: OnPremExportResult | undefined;
  currentBuild: OnPremBuild | undefined;
  downloadableArtifact: OnPremArtifact | undefined;
  registryReference: string;
  registrySecretName: string;
  registryArtifact: OnPremArtifact | undefined;
  refreshing: boolean;
  pushing: boolean;
  onRegistryReferenceChange: (value: string) => void;
  onRegistrySecretNameChange: (value: string) => void;
  onRefreshBuild: () => void;
  onPushRegistry: () => void;
}) {
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 rounded-md border p-4 text-sm">
        <span className="text-muted-foreground">Release</span>
        <span>{values.releaseVersion}</span>
        <span className="text-muted-foreground">Target</span>
        <span>
          {values.architecture} · {values.orchestrator} · {values.topology}
        </span>
        <span className="text-muted-foreground">Resources</span>
        <span>
          {values.cpu} CPU · {values.memoryGiB} GiB RAM · {values.diskGiB} GiB
          disk
        </span>
        <span className="text-muted-foreground">Data</span>
        <span>
          {values.database} · {values.storage}
        </span>
        <span className="text-muted-foreground">Source documents</span>
        <span>{values.sourceFileIds.length}</span>
        <span className="text-muted-foreground">Security</span>
        <span>
          {values.signing ? "signed" : "checksums only"} · {values.tls}
        </span>
        <span className="text-muted-foreground">Brand</span>
        <span>
          {values.organizationName || "—"} / {values.solutionName}
        </span>
      </div>

      {!validation && (
        <p className="text-sm text-muted-foreground">
          Run static validation before creating the immutable release.
        </p>
      )}
      {validation?.errors.map((error) => (
        <p key={error} className="text-sm text-destructive">
          {error}
        </p>
      ))}
      {validation?.warnings.map((warning) => (
        <p key={warning} className="text-sm text-muted-foreground">
          {warning}
        </p>
      ))}
      {validated && (
        <p className="text-sm text-primary">
          Validation passed. API version:{" "}
          {String(
            (validation?.manifest?.api as Record<string, unknown> | undefined)
              ?.version ?? "v1",
          )}
        </p>
      )}
      {exportResult && (
        <div className="space-y-4 rounded-md border border-primary p-4 text-sm">
          <p>
            Release {exportResult.release.release_version} was created. Build
            status: {currentBuild?.status}.
          </p>
          {currentBuild?.status !== "succeeded" && (
            <Button
              variant="outline"
              onClick={onRefreshBuild}
              disabled={refreshing}
            >
              {refreshing ? "Refreshing…" : "Refresh build"}
            </Button>
          )}
          {currentBuild?.status === "succeeded" && downloadableArtifact && (
            <a
              className="inline-flex h-8 items-center rounded-md border border-input px-4 text-sm hover:bg-muted"
              href={getOnPremArtifactDownloadUrl(
                exportResult.release.id,
                currentBuild.id,
                downloadableArtifact.id,
              )}
            >
              Download verified tar
            </a>
          )}
          {currentBuild?.status === "succeeded" && (
            <div className="grid grid-cols-2 gap-3 border-t pt-4">
              <Field
                id="on-prem-registry-reference"
                label="Registry image reference"
                value={registryReference}
                onChange={onRegistryReferenceChange}
              />
              <Field
                id="on-prem-registry-secret"
                label="Credential secret name"
                value={registrySecretName}
                onChange={onRegistrySecretNameChange}
              />
              <Button
                className="col-span-2"
                variant="outline"
                onClick={onPushRegistry}
                disabled={
                  !registryReference.trim() ||
                  !registrySecretName.trim() ||
                  pushing
                }
              >
                {pushing ? "Pushing…" : "Push to private registry"}
              </Button>
              {registryArtifact && (
                <p className="col-span-2 break-all text-muted-foreground">
                  Pushed {registryArtifact.location}@{registryArtifact.digest}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
