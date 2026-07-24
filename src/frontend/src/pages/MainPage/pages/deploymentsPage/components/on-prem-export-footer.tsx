import { Button } from "@/components/ui/button";

export function OnPremExportFooter({
  step,
  exported,
  canContinue,
  validated,
  validating,
  exporting,
  onBack,
  onNext,
  onValidate,
  onSubmit,
  onDone,
}: {
  step: number;
  exported: boolean;
  canContinue: boolean;
  validated: boolean;
  validating: boolean;
  exporting: boolean;
  onBack: () => void;
  onNext: () => void;
  onValidate: () => void;
  onSubmit: () => void;
  onDone: () => void;
}) {
  if (exported) return <Button onClick={onDone}>Done</Button>;

  return (
    <>
      <Button
        variant="outline"
        onClick={onBack}
        disabled={validating || exporting}
      >
        {step === 0 ? "Cancel" : "Back"}
      </Button>
      {step < 3 ? (
        <Button onClick={onNext} disabled={!canContinue}>
          Next
        </Button>
      ) : (
        <>
          <Button
            variant="outline"
            onClick={onValidate}
            disabled={validating || exporting}
          >
            {validating ? "Validating…" : "Validate"}
          </Button>
          <Button onClick={onSubmit} disabled={!validated || exporting}>
            {exporting ? "Starting build…" : "Create release & build"}
          </Button>
        </>
      )}
    </>
  );
}
