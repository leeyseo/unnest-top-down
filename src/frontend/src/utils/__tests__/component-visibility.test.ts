import type { APIObjectType, ComponentVisibility } from "@/types/api";
import {
  filterComponentCatalog,
  isComponentVisible,
} from "../component-visibility";

const component = (displayName: string) =>
  ({ display_name: displayName }) as APIObjectType[string][string];

const catalog: APIObjectType = {
  openai: {
    ChatOpenAIComponent: component("OpenAI Chat"),
    OpenAIEmbeddingsComponent: component("OpenAI Embeddings"),
  },
  anthropic: {
    ChatAnthropicComponent: component("Anthropic Chat"),
  },
};

const visibility: ComponentVisibility = {
  user_id: "user-id",
  hidden_bundles: ["anthropic"],
  hidden_components: ["openai.OpenAIEmbeddingsComponent"],
};

describe("component visibility", () => {
  it("filters hidden Bundles and Components without mutating the catalog", () => {
    const filtered = filterComponentCatalog(catalog, visibility);

    expect(filtered.anthropic).toBeUndefined();
    expect(filtered.openai.ChatOpenAIComponent).toBeDefined();
    expect(filtered.openai.OpenAIEmbeddingsComponent).toBeUndefined();
    expect(catalog.anthropic.ChatAnthropicComponent).toBeDefined();
    expect(catalog.openai.OpenAIEmbeddingsComponent).toBeDefined();
  });

  it("treats new catalog entries as visible by default", () => {
    expect(
      isComponentVisible("openai", "FutureModelComponent", visibility),
    ).toBe(true);
  });
});
