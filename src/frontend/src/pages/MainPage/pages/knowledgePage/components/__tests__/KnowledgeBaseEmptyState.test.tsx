import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import KnowledgeBaseEmptyState from "../KnowledgeBaseEmptyState";

// Mock dependencies
jest.mock("@/stores/alertStore", () => ({
  __esModule: true,
  default: jest.fn((selector) =>
    selector({
      setSuccessData: jest.fn(),
      setErrorData: jest.fn(),
    }),
  ),
}));

const mockCaptureSubmit = jest.fn();
const mockApplyOptimisticUpdate = jest.fn().mockReturnValue(true);

jest.mock("../../hooks/useOptimisticKnowledgeBase", () => ({
  useOptimisticKnowledgeBase: () => ({
    captureSubmit: mockCaptureSubmit,
    applyOptimisticUpdate: mockApplyOptimisticUpdate,
  }),
}));

// Mock the modal component
jest.mock("@/modals/knowledgeBaseUploadModal/KnowledgeBaseUploadModal", () => {
  return function MockKnowledgeBaseUploadModal({
    open,
    setOpen,
    onSubmit,
  }: {
    open: boolean;
    setOpen: (open: boolean) => void;
    onSubmit: (data: {
      sourceName: string;
      files: File[];
      embeddingModel: null;
    }) => void;
  }) {
    return open ? (
      <div data-testid="upload-modal">
        <button data-testid="modal-close" onClick={() => setOpen(false)}>
          Close
        </button>
        <button
          data-testid="modal-submit"
          onClick={() => {
            onSubmit({
              sourceName: "TestKB",
              files: [new File(["content"], "test.txt")],
              embeddingModel: null,
            });
            setOpen(false);
          }}
        >
          Submit
        </button>
      </div>
    ) : null;
  };
});

jest.mock("@/components/common/genericIconComponent", () => {
  return function MockIcon() {
    return <span data-testid="mock-icon" />;
  };
});

const createTestWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

describe("KnowledgeBaseEmptyState", () => {
  const mockHandleCreateKnowledge = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("renders empty state message correctly", () => {
    render(
      <KnowledgeBaseEmptyState
        handleCreateKnowledge={mockHandleCreateKnowledge}
      />,
      { wrapper: createTestWrapper() },
    );

    expect(
      screen.getByText("Give your agents something to know"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Add documents and turn them into searchable knowledge/),
    ).toBeInTheDocument();
  });

  it("renders Add Knowledge button", () => {
    render(
      <KnowledgeBaseEmptyState
        handleCreateKnowledge={mockHandleCreateKnowledge}
      />,
      { wrapper: createTestWrapper() },
    );

    const addButton = screen.getByText("Add Knowledge");
    expect(addButton).toBeInTheDocument();
  });

  it("opens modal when Add Knowledge button is clicked", async () => {
    const user = userEvent.setup();
    render(
      <KnowledgeBaseEmptyState
        handleCreateKnowledge={mockHandleCreateKnowledge}
      />,
      { wrapper: createTestWrapper() },
    );

    await user.click(screen.getByRole("button", { name: "Add Knowledge" }));

    expect(screen.getByTestId("upload-modal")).toBeInTheDocument();
  });

  it("calls captureSubmit when form is submitted", async () => {
    const user = userEvent.setup();
    render(
      <KnowledgeBaseEmptyState
        handleCreateKnowledge={mockHandleCreateKnowledge}
      />,
      { wrapper: createTestWrapper() },
    );

    await user.click(screen.getByRole("button", { name: "Add Knowledge" }));
    await user.click(screen.getByRole("button", { name: "Submit" }));

    expect(mockCaptureSubmit).toHaveBeenCalledWith({
      sourceName: "TestKB",
      files: expect.any(Array),
      embeddingModel: null,
    });
  });

  it("calls applyOptimisticUpdate when modal closes after submission", async () => {
    const user = userEvent.setup();
    render(
      <KnowledgeBaseEmptyState
        handleCreateKnowledge={mockHandleCreateKnowledge}
      />,
      { wrapper: createTestWrapper() },
    );

    await user.click(screen.getByRole("button", { name: "Add Knowledge" }));
    await user.click(screen.getByRole("button", { name: "Submit" }));

    expect(mockApplyOptimisticUpdate).toHaveBeenCalled();
  });

  it("closes modal without calling applyOptimisticUpdate when closed without submission", async () => {
    const user = userEvent.setup();
    mockApplyOptimisticUpdate.mockClear();

    render(
      <KnowledgeBaseEmptyState
        handleCreateKnowledge={mockHandleCreateKnowledge}
      />,
      { wrapper: createTestWrapper() },
    );

    await user.click(screen.getByRole("button", { name: "Add Knowledge" }));

    expect(screen.getByTestId("upload-modal")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close" }));

    // Modal should call applyOptimisticUpdate even on close (it returns false if no submission)
    expect(mockApplyOptimisticUpdate).toHaveBeenCalled();
  });
});
