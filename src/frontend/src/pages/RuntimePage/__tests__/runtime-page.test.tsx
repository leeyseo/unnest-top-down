import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mockGet = jest.fn();
const mockPost = jest.fn();

jest.mock("@/controllers/API/runtime-api", () => ({
  runtimeApi: {
    get: mockGet,
    post: mockPost,
    delete: jest.fn(),
  },
}));

import RuntimePage from "../index";

const setupStatus = {
  complete: false,
  release_version: "1.0.0",
  api_versions: ["v1"],
  license: { valid: true, reason: null },
  required_secret_names: [],
  configured_secret_names: [],
  branding: {
    solution_name: "Unnest Runtime",
    show_unnest_branding: true,
  },
};

describe("RuntimePage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGet.mockResolvedValue({ data: setupStatus });
  });

  it("loads only runtime setup state before initial configuration", async () => {
    render(<RuntimePage />);

    expect(
      await screen.findByRole("heading", { name: "Unnest Runtime 초기 설정" }),
    ).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith("/api/v1/setup/status");
    expect(mockGet).not.toHaveBeenCalledWith(
      expect.stringMatching(/flows|components|starter-projects|projects/),
    );
    expect(screen.getByText("Powered by Unnest")).toBeInTheDocument();
  });

  it("requires the one-time recovery identity to be saved before login", async () => {
    mockPost.mockResolvedValue({
      data: {
        ...setupStatus,
        complete: true,
        recovery_identity: "AGE-SECRET-KEY-1TEST",
      },
    });
    render(<RuntimePage />);
    await screen.findByRole("heading", { name: "Unnest Runtime 초기 설정" });

    fireEvent.change(screen.getByLabelText(/Admin username/), {
      target: { value: "runtime-admin" },
    });
    for (const input of screen.getAllByLabelText(/Password|Confirm/)) {
      fireEvent.change(input, { target: { value: "strong-password" } });
    }
    fireEvent.click(screen.getByRole("button", { name: /Complete setup/ }));

    expect(
      await screen.findByRole("heading", { name: /Save the recovery key now/ }),
    ).toBeInTheDocument();
    const continueButton = screen.getByRole("button", {
      name: /Continue to sign in/,
    });
    expect(continueButton).toBeDisabled();
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/api/v1/setup", {
        admin_username: "runtime-admin",
        admin_password: "strong-password",
        model_endpoint: null,
        storage_endpoint: null,
        tls_certificate_configured: false,
        secret_values: {},
      }),
    );
  });
});
