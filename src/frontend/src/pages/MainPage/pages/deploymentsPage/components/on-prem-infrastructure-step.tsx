import type {
  OnPremWizardUpdate,
  OnPremWizardValues,
} from "../helpers/on-prem-release";
import { Field, SelectField, ToggleField } from "./on-prem-export-fields";

const ORCHESTRATOR_OPTIONS = [
  { value: "compose", label: "Docker / Podman Compose" },
  { value: "helm", label: "Kubernetes / Helm" },
];
const TOPOLOGY_OPTIONS = [
  { value: "single", label: "Single server" },
  { value: "ha", label: "Kubernetes HA" },
];
const ARCHITECTURE_OPTIONS = [
  { value: "amd64", label: "AMD64" },
  { value: "arm64", label: "ARM64" },
];
const ACCELERATOR_OPTIONS = [
  { value: "cpu", label: "CPU" },
  { value: "nvidia", label: "NVIDIA GPU" },
  { value: "amd", label: "AMD ROCm" },
];
const DATABASE_OPTIONS = [
  { value: "embedded-postgresql", label: "Bundled" },
  { value: "external-postgresql", label: "Institution" },
];
const STORAGE_OPTIONS = [
  { value: "local", label: "Local volume" },
  { value: "minio", label: "MinIO" },
  { value: "s3", label: "S3 compatible" },
];
const INFRASTRUCTURE_OPTIONS = [
  { value: "bundled", label: "Bundled services" },
  { value: "external", label: "Institution services" },
];
const MODEL_RUNTIME_OPTIONS = [
  { value: "external", label: "External endpoint" },
  { value: "bundled", label: "Bundled local model" },
];

export function OnPremInfrastructureStep({
  values,
  update,
}: {
  values: OnPremWizardValues;
  update: OnPremWizardUpdate;
}) {
  return (
    <div className="grid grid-cols-2 gap-4">
      <SelectField
        label="Package"
        value={values.orchestrator}
        options={ORCHESTRATOR_OPTIONS}
        onChange={(value) =>
          update("orchestrator", value as "compose" | "helm")
        }
        disabled={values.topology === "ha"}
      />
      <SelectField
        label="Topology"
        value={values.topology}
        options={TOPOLOGY_OPTIONS}
        onChange={(value) => {
          update("topology", value as "single" | "ha");
          if (value === "ha") update("orchestrator", "helm");
        }}
      />
      <SelectField
        label="Architecture"
        value={values.architecture}
        options={ARCHITECTURE_OPTIONS}
        onChange={(value) => update("architecture", value as "amd64" | "arm64")}
      />
      <SelectField
        label="Accelerator"
        value={values.accelerator}
        options={ACCELERATOR_OPTIONS}
        onChange={(value) =>
          update("accelerator", value as "cpu" | "nvidia" | "amd")
        }
      />
      <SelectField
        label="PostgreSQL"
        value={values.database}
        options={DATABASE_OPTIONS}
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
        options={STORAGE_OPTIONS}
        onChange={(value) =>
          update("storage", value as "local" | "minio" | "s3")
        }
      />
      <SelectField
        label="Infrastructure"
        value={values.infrastructure}
        options={INFRASTRUCTURE_OPTIONS}
        onChange={(value) =>
          update("infrastructure", value as "bundled" | "external")
        }
      />
      <SelectField
        label="Model runtime"
        value={values.modelRuntime}
        options={MODEL_RUNTIME_OPTIONS}
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
  );
}
