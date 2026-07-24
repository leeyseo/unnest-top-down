import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { FileType } from "@/types/file_management";
import { defaultOnPremWizardValues } from "../../helpers/on-prem-release";
import { OnPremFlowsApiStep } from "../on-prem-flows-api-step";

const files: FileType[] = [
  {
    id: "source-1",
    user_id: "user-1",
    provider: "local",
    name: "guide.txt",
    path: "user-1/guide.txt",
    created_at: "2026-07-24T00:00:00Z",
    size: 2048,
  },
  {
    id: "source-2",
    user_id: "user-1",
    provider: "local",
    name: "<script>alert(1)</script>.txt",
    path: "user-1/safe.txt",
    created_at: "2026-07-24T00:00:00Z",
    size: 8,
  },
];

type RenderOptions = {
  sourceFileIds?: string[];
  availableFiles?: FileType[];
  filesLoading?: boolean;
  filesError?: boolean;
};

function renderStep({
  sourceFileIds = [],
  availableFiles = files,
  filesLoading = false,
  filesError = false,
}: RenderOptions = {}) {
  const update = jest.fn();
  render(
    <OnPremFlowsApiStep
      values={{ ...defaultOnPremWizardValues, sourceFileIds }}
      update={update}
      flowChoices={[]}
      agentFlowId=""
      ingestionFlowId=""
      onAgentFlowChange={jest.fn()}
      onIngestionFlowChange={jest.fn()}
      agentVersions={[]}
      ingestionVersions={[]}
      files={availableFiles}
      filesLoading={filesLoading}
      filesError={filesError}
      jsonError=""
    />,
  );
  return update;
}

describe("OnPremFlowsApiStep source documents", () => {
  beforeEach(() => jest.clearAllMocks());

  it("adds an SI-owned file to the release selection", async () => {
    const user = userEvent.setup();
    const update = renderStep();

    await user.click(screen.getByLabelText(/guide\.txt/));

    expect(update).toHaveBeenCalledWith("sourceFileIds", ["source-1"]);
  });

  it("removes a selected file without changing the other selections", async () => {
    const user = userEvent.setup();
    const update = renderStep({ sourceFileIds: ["source-1", "source-2"] });

    await user.click(screen.getByLabelText(/guide\.txt/));

    expect(update).toHaveBeenCalledWith("sourceFileIds", ["source-2"]);
  });

  it("renders untrusted filenames as text", () => {
    renderStep();

    expect(
      screen.getByText("<script>alert(1)</script>.txt"),
    ).toBeInTheDocument();
    expect(document.querySelector("script")).not.toBeInTheDocument();
  });

  it("shows a recovery message when the files query fails", () => {
    renderStep({ availableFiles: [], filesError: true });

    expect(
      screen.getByText("Source files could not be loaded."),
    ).toBeInTheDocument();
  });

  it("directs the user to upload files when none are available", () => {
    renderStep({ availableFiles: [] });

    expect(
      screen.getByText(
        "Upload source files from the Files page before creating the release.",
      ),
    ).toBeInTheDocument();
  });

  it("prevents selecting more than 1,000 source documents", () => {
    renderStep({
      sourceFileIds: Array.from(
        { length: 1000 },
        (_, index) => `selected-${index}`,
      ),
      availableFiles: [files[0]],
    });

    expect(screen.getByTestId("on-prem-source-source-1")).toBeDisabled();
    expect(
      screen.getByText("The maximum of 1000 source documents is selected."),
    ).toBeInTheDocument();
  });
});
