export function convertTestName(name: string): string {
  return name.replaceAll(" ", "-").toLowerCase();
}
