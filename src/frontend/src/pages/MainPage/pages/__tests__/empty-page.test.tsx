import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UnnestWelcomeEmptyState } from "../empty-page";

interface ButtonProps {
  children?: React.ReactNode;
  onClick?: () => void;
  "data-testid"?: string;
  [key: string]: unknown;
}

interface IconProps {
  name: string;
  [key: string]: unknown;
}

interface WrapperProps {
  children: React.ReactNode;
  [key: string]: unknown;
}

// startNewFlow mock shared across the suite so assertions can inspect it.
const startNewFlowMock = jest.fn();

jest.mock(
  "@/components/core/flowBuilderWelcome/hooks/use-start-new-flow",
  () => ({
    useStartNewFlow: () => startNewFlowMock,
  }),
);

jest.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
  initReactI18next: { type: "3rdParty", init: jest.fn() },
}));

jest.mock("@/components/common/genericIconComponent", () => ({
  ForwardedIconComponent: ({ name }: IconProps) => (
    <div data-testid={`icon-${name}`}>{name}</div>
  ),
}));

jest.mock("@/components/core/cardsWrapComponent", () => ({
  __esModule: true,
  default: ({ children }: WrapperProps) => <div>{children}</div>,
}));

jest.mock("@/components/ui/dot-background", () => ({
  DotBackgroundDemo: ({ children }: WrapperProps) => <div>{children}</div>,
}));

jest.mock("@/components/ui/button", () => ({
  Button: ({
    children,
    onClick,
    "data-testid": testId,
    ...props
  }: ButtonProps) => (
    <button onClick={onClick} data-testid={testId} {...props}>
      {children}
    </button>
  ),
}));

jest.mock("@/stores/foldersStore", () => ({
  useFolderStore: (selector: (s: { folders: unknown[] }) => unknown) =>
    selector({ folders: [] }),
}));

jest.mock("../../hooks/use-on-file-drop", () => ({
  __esModule: true,
  default: () => jest.fn(),
}));

describe("UnnestWelcomeEmptyState", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("should start a new flow when the primary action is clicked", async () => {
    const user = userEvent.setup();
    render(<UnnestWelcomeEmptyState />);

    await user.click(
      screen.getByRole("button", { name: /page\.createFirstFlow/ }),
    );

    expect(startNewFlowMock).toHaveBeenCalledTimes(1);
  });
});
