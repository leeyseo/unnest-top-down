import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type {
  OnPremWizardUpdate,
  OnPremWizardValues,
} from "../helpers/on-prem-release";
import { Field, SelectField, ToggleField } from "./on-prem-export-fields";

const TOGGLES = [
  ["signing", "Sign artifacts"],
  ["backups", "Include backup support"],
  ["monitoring", "Include monitoring"],
  ["clamav", "Include ClamAV"],
  ["grafana", "Include Grafana"],
  ["pinned", "Pin this release"],
  ["allowLanguageSwitch", "Allow language switching"],
  ["showUnnestBranding", "Show Unnest branding"],
  ["storeConversations", "Store conversations"],
] as const;
const TLS_OPTIONS = [
  { value: "institution", label: "Institution certificate" },
  { value: "self-signed", label: "Self-signed (initial setup)" },
];
const LANGUAGE_OPTIONS = [
  { value: "ko", label: "한국어" },
  { value: "en", label: "English" },
];

export function OnPremOperationsStep({
  values,
  update,
}: {
  values: OnPremWizardValues;
  update: OnPremWizardUpdate;
}) {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4">
        <SelectField
          label="TLS certificate"
          value={values.tls}
          options={TLS_OPTIONS}
          onChange={(value) =>
            update("tls", value as "institution" | "self-signed")
          }
        />
        <SelectField
          label="Default language"
          value={values.defaultLanguage}
          options={LANGUAGE_OPTIONS}
          onChange={(value) => update("defaultLanguage", value as "ko" | "en")}
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
          id="on-prem-logo-url"
          label="Logo URL"
          type="url"
          value={values.logoUrl}
          onChange={(value) => update("logoUrl", value)}
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
        {TOGGLES.map(([key, label]) => (
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
            onChange={(event) => update("secretNames", event.target.value)}
          />
        </div>
      </div>
    </div>
  );
}
