import { useState } from "react";
import ForwardedIconComponent from "@/components/common/genericIconComponent";
import { Button } from "@/components/ui/button";
import OnPremExportModal from "../deploymentsPage/components/on-prem-export-modal";

export default function OnPremExportPage() {
  const [open, setOpen] = useState(false);

  return (
    <div className="flex flex-col gap-6 pt-4">
      <div>
        <h2 className="text-lg font-semibold">On-prem Export</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Validate an immutable Unnest release for an offline installation.
          Package builds will be available after a BuildKit server is
          configured.
        </p>
      </div>
      <div>
        <Button onClick={() => setOpen(true)} data-testid="open-on-prem-export">
          <ForwardedIconComponent name="Package" className="h-4 w-4" />
          Configure export
        </Button>
      </div>
      <OnPremExportModal open={open} setOpen={setOpen} />
    </div>
  );
}
