---
name: profile-avatar-creator
description: Create, expand, or replace Unnest/Langflow profile avatar packs under initial_setup/profile_pictures. Use for generated bird characters, round nature scenes, transparent 512x512 profile images, category migrations, stale bundled-asset filtering, profile API tests, or Playwright screenshots of the profile chooser.
---

# Profile Avatar Creator

Create profile packs that are visually consistent, safe inside round UI crops, backward-compatible with upgraded installations, and verified in the real settings screen.

## Asset contract

- Keep internal category names stable unless the user explicitly requests a migration.
- Use `Birds` for character avatars and `Space` for nature scenes in this repository.
- Name files with a stable numeric prefix: `NN-short-slug.png`.
- Generate new raster assets as 512x512 RGBA PNG.
- Keep all four corner pixels fully transparent.
- Center characters with generous padding and no background, frame, text, logo, or shadow.
- Render scenes as circular vignettes occupying roughly 75–80% of the canvas with transparent space outside.
- Preserve existing SVG assets unless the user requests replacement.
- Never overwrite an existing asset without explicit authorization.

## Workflow

1. Check `git status`, the recent checkpoint, current asset folders, API references, translations, and focused tests.
2. Agree on an explicit roster. Generate one image per subject; do not use a contact sheet as the production source.
3. When generating images, read and follow the installed `imagegen` skill. Use its built-in tool by default.
4. Match the established retro pixel-art pet style. Use a perfectly flat `#ff00ff` chroma-key background, generous padding, no shadow, and no extra objects.
5. Copy raw outputs into `tmp/imagegen/profile-avatars/raw/`. Keep generated service originals unchanged.
6. Prepare each production asset:

   ```bash
   python3 .agents/skills/profile-avatar-creator/scripts/prepare_avatar.py \
     --input tmp/imagegen/profile-avatars/raw/06-kingfisher.png \
     --output src/backend/base/langflow/initial_setup/profile_pictures/Birds/06-kingfisher.png \
     --kind character
   ```

   Use `--kind scene` for circular landscapes. Add `--force` only when replacement was explicitly requested.

7. Inspect every result with `view_image`. Reject incorrect species, inconsistent style, clipped silhouettes, square scene edges, chroma fringe, or poor 48px readability.
8. Update integration only as needed:
   - Keep current bundled folders in the profile API allowlist.
   - Treat package filenames as the manifest for built-in folders so stale files in persistent config directories stay hidden.
   - Continue allowing operator-created custom folders.
   - Update initial setup copying, category translations, and user-value migration when paths change.
   - Sort returned filenames for deterministic UI ordering.
9. Run focused checks:

   ```bash
   uv run ruff check src/backend/base/langflow/api/v1/files.py \
     src/backend/base/langflow/initial_setup/setup.py \
     src/backend/tests/unit/api/v1/test_files.py \
     src/backend/tests/unit/test_initial_setup.py \
     src/backend/tests/unit/test_user.py
   uv run pytest src/backend/tests/unit/api/v1/test_files.py -k profile_picture -q
   uv run pytest src/backend/tests/unit/test_initial_setup.py -k profile_picture -q
   uv run pytest src/backend/tests/unit/test_user.py -k profile_picture -q
   ```

10. For UI verification, read `.agents/skills/e2e-testing/SKILL.md`, launch the current backend and frontend against temporary state, and use Playwright to assert category counts before capturing the settings card.
11. Stage only product assets, code, and tests. Exclude `tmp/` and `artifacts/`. Create a Git checkpoint after verification.

## Prompt patterns

Character:

```text
Create one <species> matching the established cute retro pixel-art pet style:
full body, three-quarter view, centered, generous padding, recognizable species
colors, crisp dark outline, limited palette. Use a perfectly flat #ff00ff
chroma-key background. One bird only; no shadow, branch, frame, text, or logo.
```

Scene:

```text
Create one round retro pixel-art nature vignette of <scene>, readable at 48px,
centered at 78% canvas size with a dark outline and limited natural palette.
Outside the circle use perfectly flat #ff00ff. No square frame, text, logo,
people, buildings, or cast shadow.
```

## Completion report

Report the final directories, asset counts, generated subjects, prompt pattern, image-generation mode, validation results, Playwright screenshot path, and Git commit. Explicitly state whether legacy files were deleted, hidden, or preserved.
