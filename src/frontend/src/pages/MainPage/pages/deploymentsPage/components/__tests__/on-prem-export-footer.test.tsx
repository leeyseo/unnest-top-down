import { fireEvent, render, screen } from "@testing-library/react";
import { OnPremExportFooter } from "../on-prem-export-footer";

test("allows validation without submitting a disabled build", () => {
  const onValidate = jest.fn();
  const onSubmit = jest.fn();

  render(
    <OnPremExportFooter
      step={3}
      exported={false}
      canContinue
      validated
      buildsEnabled={false}
      validating={false}
      exporting={false}
      onBack={jest.fn()}
      onNext={jest.fn()}
      onValidate={onValidate}
      onSubmit={onSubmit}
      onDone={jest.fn()}
    />,
  );

  fireEvent.click(screen.getByText("Validate"));
  fireEvent.click(screen.getByTestId("create-on-prem-build"));

  expect(onValidate).toHaveBeenCalledTimes(1);
  expect(onSubmit).not.toHaveBeenCalled();
  expect(screen.getByTestId("create-on-prem-build")).toBeDisabled();
});
