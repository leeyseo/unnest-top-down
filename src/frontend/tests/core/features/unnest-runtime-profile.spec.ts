import { expect, test } from "../../fixtures";

const setupStatus = {
  complete: false,
  release_version: "1.0.0",
  api_versions: ["v1"],
  branding: {
    solution_name: "Government Agent",
    organization_name: "Government Agency",
    show_unnest_branding: true,
  },
  default_language: "ko",
  allow_language_switch: true,
  license: { valid: true, reason: null },
  required_secret_names: [],
  configured_secret_names: [],
};

test(
  "on-prem runtime exposes setup and never the flow editor",
  { tag: ["@release", "@api", "@regression"] },
  async ({ page }) => {
    await page.route("**/api/v1/setup/status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(setupStatus),
      }),
    );

    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Government Agent 초기 설정" }),
    ).toBeVisible();
    await expect(page.getByTestId("mainpage_title")).toHaveCount(0);

    await page.goto("/flows");
    await expect(page).toHaveURL("/");
    await expect(
      page.getByRole("heading", { name: "Government Agent 초기 설정" }),
    ).toBeVisible();

    await page.getByLabel("관리자 ID / Admin username").fill("runtime-admin");
    await page.getByLabel("비밀번호 / Password").fill("valid-password");
    await page.getByLabel("비밀번호 확인 / Confirm").fill("different-password");
    await page.getByRole("button", { name: /Complete setup/ }).click();

    await expect(page.getByRole("alert")).toContainText(
      "Passwords do not match",
    );
  },
);
