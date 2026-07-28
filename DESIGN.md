---
version: alpha
name: Unnest
description: AI workflow를 시각적으로 설계하고 실행하는 플랫폼
colors:
  # Core UI palette
  primary: "#3F6212"
  primary-foreground: "#FFFFFF"
  primary-hover: "#365314"
  background: "#FFFFFF"
  foreground: "#000000"
  muted: "#F4F4F5"
  muted-foreground: "#71717A"
  border: "#E4E4E7"
  input: "#E4E4E7"
  ring: "#3F6212"
  card: "#FFFFFF"
  card-foreground: "#000000"
  popover: "#FFFFFF"
  popover-foreground: "#000000"
  secondary: "#FFFFFF"
  secondary-foreground: "#27272A"
  secondary-hover: "#E4E4E7"
  accent: "#F4F4F5"
  accent-foreground: "#000000"
  placeholder: "#A1A1AA"
  placeholder-foreground: "#A1A1AA"
  tooltip: "#000000"
  tooltip-foreground: "#FFFFFF"

  # Semantic status
  destructive: "#DC2626"
  destructive-foreground: "#FFFFFF"
  error: "#991B1B"
  error-background: "#FEF2F2"
  error-foreground: "#991B1B"
  success-background: "#F0FDF4"
  success-foreground: "#14532D"
  info-background: "#F0F4FD"
  info-foreground: "#141653"
  warning: "#FCE68A"
  warning-foreground: "#18181B"
  warning-text: "#FFFFFF"

  # Status indicators
  status-red: "#EF4444"
  status-green: "#4ADE80"
  status-yellow: "#EAB308"
  status-blue: "#2563EB"
  status-gray: "#6B7280"

  # Accent families (background / foreground pairs)
  accent-emerald: "#D1F9E4"
  accent-emerald-foreground: "#047857"
  accent-emerald-hover: "#A7F3D0"
  accent-indigo: "#E0E7FF"
  accent-indigo-foreground: "#4F46E5"
  accent-pink: "#FCE8F3"
  accent-pink-foreground: "#BE185D"
  accent-amber: "#FCE68A"
  accent-amber-foreground: "#B45309"

  # Standalone accent references (not bg/fg pairs -- used for text, icons, links)
  accent-blue: "#3B82F6"
  accent-blue-muted: "#D5E4FF"
  accent-blue-muted-foreground: "#51A2FF"
  accent-purple-foreground: "#9333EA"
  accent-purple-muted: "#EDD5FF"
  accent-purple-muted-foreground: "#C27AFF"
  accent-red-foreground: "#DC2626"

  # Indigo scale
  high-indigo: "#4338CA"
  medium-indigo: "#6366F1"
  low-indigo: "#E0E7FF"

  # Canvas and node system
  canvas: "#F4F4F5"
  canvas-dot: "#A1A1AA"
  node-selected: "#3F6212"
  node-ring: "#E4E4E7"
  connection: "#A1A1AA"
  hover: "#F2F4F5"
  selected: "#3F6212"

  # Code blocks
  code-background: "#18181B"
  code-foreground: "#E4E4E7"

  # Utility
  hard-zinc: "#51515A"
  smooth-red: "#FDE1E1"

  # Note colors (sticky notes on canvas)
  note-amber: "#FCE68A"
  note-neutral: "#E4E4E7"
  note-rose: "#FECDD3"
  note-blue: "#BFDBFE"
  note-lime: "#D9F99D"

  # Beta feature badge
  beta-background: "#ECFCCB"
  beta-foreground: "#3F6212"

  # Data type colors (node port type indicators -- light mode)
  # In light mode: base = saturated, foreground = light tint
  datatype-pink: "#DB2777"
  datatype-pink-foreground: "#FBE6F2"
  datatype-rose: "#E11D48"
  datatype-rose-foreground: "#FFE4E6"
  datatype-yellow: "#CA8A04"
  datatype-yellow-foreground: "#FEF9C3"
  datatype-blue: "#2563EB"
  datatype-blue-foreground: "#DBEAFE"
  datatype-gray: "#4B5563"
  datatype-gray-foreground: "#F3F4F6"
  datatype-lime: "#65A30D"
  datatype-lime-foreground: "#ECFCCB"
  datatype-red: "#DC2626"
  datatype-red-foreground: "#FEE2E2"
  datatype-violet: "#7C3AED"
  datatype-violet-foreground: "#EDE9FE"
  datatype-emerald: "#059669"
  datatype-emerald-foreground: "#D1FAE5"
  datatype-fuchsia: "#C026D3"
  datatype-fuchsia-foreground: "#FAE8FF"
  datatype-purple: "#9333EA"
  datatype-purple-foreground: "#F3E8FF"
  datatype-cyan: "#0891B2"
  datatype-cyan-foreground: "#CFFAFE"
  datatype-indigo: "#4F46E5"
  datatype-indigo-foreground: "#E0E7FF"
  datatype-orange: "#EA580C"
  datatype-orange-foreground: "#FFEDD5"

  # Gradient / neon accents (flow gradients, tool-mode effects)
  neon-fuchsia: "#FF3276"
  digital-orchid: "#F480FF"
  plasma-purple: "#7C3AED"
  electric-blue: "#3B10FD"
  holo-frost: "#9AF3FD"
  terminal-green: "#9AFDA9"
  cosmic-void: "#1A0250"

  # Brand-specific UI
  component-icon: "#3B82F6"
  flow-icon: "#1D4ED8"
  chat-bot-icon: "#AFE6EF"
  chat-user-icon: "#AFACE9"
  build-trigger: "#DC735B"
  chat-trigger: "#5C8BE1"
  chat-send: "#000000"
  ice: "#31A3CC"

  # Sidebar (dark mode only -- light mode inherits core tokens)
  sidebar-background: "#18181B"
  sidebar-foreground: "#F4F4F5"
  sidebar-primary: "#1D4ED8"
  sidebar-primary-foreground: "#FFFFFF"
  sidebar-accent: "#27272A"
  sidebar-accent-foreground: "#F4F4F5"
  sidebar-border: "#27272A"
  sidebar-ring: "#3B82F6"

typography:
  headline-lg:
    fontFamily: Pretendard Variable
    fontSize: 30px
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Pretendard Variable
    fontSize: 24px
    fontWeight: 600
    lineHeight: 1.3
  headline-sm:
    fontFamily: Pretendard Variable
    fontSize: 20px
    fontWeight: 600
    lineHeight: 1.4
  body-lg:
    fontFamily: Pretendard Variable
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.6
  body-md:
    fontFamily: Pretendard Variable
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
  body-sm:
    fontFamily: Pretendard Variable
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.5
  body-xs:
    fontFamily: Pretendard Variable
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.5
  body-xxs:
    fontFamily: Pretendard Variable
    fontSize: 11px
    fontWeight: 400
    lineHeight: 1.4
  label-lg:
    fontFamily: Pretendard Variable
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.4
  label-md:
    fontFamily: Pretendard Variable
    fontSize: 13px
    fontWeight: 500
    lineHeight: 1.4
  label-sm:
    fontFamily: Pretendard Variable
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.3
  label-xs:
    fontFamily: Pretendard Variable
    fontSize: 11px
    fontWeight: 500
    lineHeight: 1.3
  code-md:
    fontFamily: JetBrains Mono
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.6
  code-sm:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.5
  display:
    fontFamily: Pretendard Variable
    fontSize: 36px
    fontWeight: 700
    lineHeight: 1.1

rounded:
  none: 0px
  sm: 4px
  md: 6px
  lg: 8px
  xl: 12px
  full: 9999px

spacing:
  unit: 4px
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 32px
  2xl: 48px
  3xl: 64px

components:
  # Buttons
  button-default:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    typography: "{typography.label-md}"
    rounded: "{rounded.lg}"
    height: 40px
    padding: 8px 16px
  button-default-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.secondary-foreground}"
    typography: "{typography.label-md}"
    rounded: "{rounded.lg}"
    height: 40px
    padding: 8px 16px
  button-secondary-hover:
    backgroundColor: "{colors.secondary-hover}"
  button-destructive:
    backgroundColor: "{colors.destructive}"
    textColor: "{colors.destructive-foreground}"
    typography: "{typography.label-md}"
    rounded: "{rounded.lg}"
    height: 40px
    padding: 8px 16px
  button-outline:
    backgroundColor: transparent
    textColor: "{colors.foreground}"
    typography: "{typography.label-md}"
    rounded: "{rounded.lg}"
    height: 40px
    padding: 8px 16px
  button-outline-hover:
    backgroundColor: "{colors.input}"
  button-ghost:
    backgroundColor: transparent
    textColor: "{colors.foreground}"
    typography: "{typography.label-md}"
    rounded: "{rounded.lg}"
    height: 40px
    padding: 8px 16px
  button-ghost-hover:
    backgroundColor: "{colors.accent}"
  button-warning:
    backgroundColor: "{colors.warning-foreground}"
    textColor: "{colors.warning-text}"
    typography: "{typography.label-md}"
    rounded: "{rounded.lg}"
    height: 40px
    padding: 8px 16px
  button-link:
    backgroundColor: transparent
    textColor: "{colors.primary}"
    typography: "{typography.label-md}"
  button-icon-md:
    backgroundColor: transparent
    rounded: "{rounded.md}"
    padding: 6px
  button-icon-sm:
    backgroundColor: transparent
    rounded: "{rounded.md}"
    padding: 2px

  # Cards
  card-default:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.card-foreground}"
    rounded: "{rounded.lg}"
    padding: 16px
  card-title:
    textColor: "{colors.foreground}"
    typography: "{typography.label-lg}"
  card-description:
    textColor: "{colors.muted-foreground}"
    typography: "{typography.body-md}"

  # Inputs
  input-field:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    typography: "{typography.body-md}"
    rounded: "{rounded.lg}"
    height: 40px
    padding: 0 12px
  input-field-hover:
    backgroundColor: "{colors.background}"
  input-node:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    typography: "{typography.body-md}"
    rounded: "{rounded.lg}"
    padding: 2px 12px

  # Badges
  badge-default:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    rounded: "{rounded.full}"
    padding: 0 10px
  badge-secondary:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.secondary-foreground}"
    rounded: "{rounded.full}"
    padding: 0 10px
  badge-emerald:
    backgroundColor: "{colors.accent-emerald}"
    textColor: "{colors.accent-emerald-foreground}"
    rounded: "{rounded.full}"
    padding: 0 10px
  badge-destructive:
    backgroundColor: "{colors.destructive}"
    textColor: "{colors.destructive-foreground}"
    rounded: "{rounded.full}"
    padding: 0 10px
  badge-pink:
    backgroundColor: "{colors.accent-pink}"
    textColor: "{colors.accent-pink-foreground}"
    rounded: "{rounded.full}"
    padding: 0 10px
  badge-purple:
    backgroundColor: "{colors.background}"
    textColor: "{colors.accent-purple-foreground}"
    rounded: "{rounded.full}"
    padding: 0 10px
  badge-error:
    backgroundColor: "{colors.error-background}"
    textColor: "{colors.error-foreground}"
    rounded: "{rounded.full}"
    padding: 0 10px

  # Tooltips
  tooltip-default:
    backgroundColor: "{colors.popover}"
    textColor: "{colors.popover-foreground}"
    typography: "{typography.body-md}"
    rounded: "{rounded.md}"
    padding: 6px 12px

  # Dialogs
  dialog-content:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.xl}"
    padding: 24px
  dialog-title:
    textColor: "{colors.foreground}"
    typography: "{typography.headline-sm}"
  dialog-description:
    textColor: "{colors.muted-foreground}"
    typography: "{typography.body-md}"

  # Select / Dropdown
  select-trigger:
    backgroundColor: transparent
    textColor: "{colors.primary}"
    typography: "{typography.body-md}"
    rounded: "{rounded.lg}"
    height: 32px
    padding: 8px 16px
  select-content:
    backgroundColor: "{colors.popover}"
    textColor: "{colors.popover-foreground}"
    rounded: "{rounded.lg}"
    padding: 4px
  select-item:
    backgroundColor: transparent
    textColor: "{colors.foreground}"
    typography: "{typography.body-md}"
    rounded: "{rounded.sm}"
    padding: 6px 32px
  select-item-hover:
    backgroundColor: "{colors.accent}"

  # Tabs
  tab-trigger:
    backgroundColor: transparent
    textColor: "{colors.muted-foreground}"
    typography: "{typography.label-md}"
    padding: 12px 6px
  tab-trigger-active:
    textColor: "{colors.primary}"

  # Form controls
  checkbox:
    backgroundColor: transparent
    textColor: "{colors.primary}"
    rounded: "{rounded.sm}"
    size: 16px
  checkbox-checked:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
  switch-track:
    backgroundColor: "{colors.input}"
    rounded: "{rounded.full}"
    width: 44px
    height: 24px
  switch-track-checked:
    backgroundColor: "{colors.primary}"
  switch-thumb:
    backgroundColor: "{colors.background}"
    rounded: "{rounded.full}"
    size: 16px

  # Alerts
  alert-default:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.lg}"
    padding: 16px
  alert-destructive:
    backgroundColor: "{colors.background}"
    textColor: "{colors.destructive}"
    rounded: "{rounded.lg}"
    padding: 16px

  # Canvas nodes
  node-card:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.lg}"
  node-card-selected:
    backgroundColor: "{colors.background}"
  node-toolbar:
    backgroundColor: "{colors.background}"
    rounded: "{rounded.xl}"
    padding: 4px
  node-toolbar-button:
    backgroundColor: transparent
    rounded: "{rounded.md}"
    padding: 6px

  # Sidebar
  sidebar-menu-button:
    backgroundColor: transparent
    textColor: "{colors.secondary-foreground}"
    typography: "{typography.body-md}"
    rounded: "{rounded.lg}"
    height: 32px
    padding: 8px
  sidebar-menu-button-active:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-foreground}"
  sidebar-menu-button-hover:
    backgroundColor: "{colors.accent}"
---

## Overview

Unnest is a platform for visually designing and running AI workflows by dragging, connecting, and configuring modular components on an infinite canvas. The design language is that of a professional developer tool -- clean, information-dense, and deliberately restrained. Color is earned, not decorative; every hue in the system carries semantic meaning.

The aesthetic sits between a code editor and a node-based creative tool. Neutral white and zinc surfaces provide the foundation, while **Nest Green** identifies primary actions and selected form controls. This restrained brand layer lets the **data type color system** (14 distinct hues encoding connection compatibility) and semantic accent families communicate meaning without competing for attention. The result is an interface that feels focused and engineered: complexity emerges from the user's composition, not from the UI itself.

Light and dark themes are fully supported via class-based toggling (`.dark` on root). The dark theme uses a deep zinc-black (#18181B) as its foundation, not a tinted dark blue, keeping the neutral character consistent across both modes.

## Colors

The palette is built in concentric layers: a neutral core, the Nest Green action color, semantic accent families, status colors, and the domain-specific data type system.

### Core Palette

- **Primary / Nest Green (#3F6212):** Primary buttons, checked controls, and focus rings in light mode, paired with white text. Dark mode uses luminous Unnest Lime (#BEF264) with deep green text (#1A2E05). Hover uses #365314 in light mode and #A3E635 in dark mode. Both pairs meet WCAG AA contrast.
- **Background (#FFFFFF):** Pure white for cards, popovers, dialogs, and content areas. Dark mode uses #18181B (zinc-900).
- **Muted (#F4F4F5):** Light zinc-gray for secondary surfaces -- canvas background, card fills, inactive states. Dark mode: #27272A.
- **Muted Foreground (#71717A):** Medium gray for captions, descriptions, metadata, and secondary text.
- **Border (#E4E4E7):** Subtle zinc border shared by inputs, cards, dividers, and node rings. Dark mode: #3F3F46.
- **Placeholder (#A1A1AA):** Input placeholder text and canvas grid dots.

### Accent Families

Accents that serve as **background / foreground pairs** for badges, tags, and contextual surfaces. Both colors in each pair must meet WCAG AA contrast (4.5:1):

- **Emerald** (#D1F9E4 / #047857): Success, completion, enabled states, "built" indicators. Hover: #A7F3D0.
- **Indigo** (#E0E7FF / #4F46E5): Node selection, active filters, focus indicators. Also used for the indigo scale (high: #4338CA, medium: #6366F1, low: #E0E7FF).
- **Blue** (#DBEAFE / #2563EB): Informational surfaces, component icons, and supporting highlights.
- **Amber** (#FCE68A / #B45309): Warnings, caution badges. The background is a pale yellow; the foreground is burnt orange.

**Standalone accent references** (used for text color, icons, and links -- not as bg/fg pairs):

- **Blue** (#3B82F6): Links, informational highlights, chat triggers, sidebar primary. Always used on a white/dark background, never as a surface fill. Muted variant (#D5E4FF) available for subtle backgrounds.
- **Purple** (#9333EA): AI/agent-related highlights. Muted variant (#EDD5FF) for subtle backgrounds.
- **Red** (#DC2626): Error-related text and icons on neutral surfaces.

### Status Colors

Used for build status indicators, connection health, and real-time feedback dots:

- **Red** (#EF4444): Error, failed, disconnected.
- **Green** (#4ADE80): Success, connected, running.
- **Yellow** (#EAB308): Warning, building, pending.
- **Blue** (#2563EB): Info, selected, active.
- **Gray** (#6B7280): Inactive, unknown, disabled.

### Data Type Color System

Unnest uses **14 distinct hues** to encode the type of data flowing through node connections. Each type has a saturated foreground and a light-tint background. In **light mode**, the base token is the saturated color (used for port dots and connection lines) and the `-foreground` token is the light tint (used for backgrounds). In **dark mode, these roles swap** -- the base becomes the light tint and the foreground becomes the saturated color. This ensures readability against both light and dark surfaces.

| Data Type | Color Name | Hex (saturated) | Used For |
|:----------|:-----------|:-----------------|:---------|
| str / Text / Message | indigo | #4F46E5 | String data, text, messages |
| Document | lime | #65A30D | Document objects |
| Data / JSON | red | #DC2626 | Structured data, JSON |
| Embeddings | emerald | #059669 | Vector embeddings |
| LanguageModel | fuchsia | #C026D3 | LLM model objects |
| Prompt | violet | #7C3AED | Prompt templates |
| Tool | cyan | #0891B2 | Tool definitions |
| Agent | purple | #9333EA | Agent objects |
| number | purple | #9333EA | Numeric values |
| DataFrame / Table | pink | #DB2777 | Tabular data |
| chains | orange | #EA580C | Chain compositions |
| memories | yellow | #CA8A04 | Memory objects |
| unknown | gray | #4B5563 | Untyped or unknown |
| inputs | emerald | #059669 | Input components |

### Note Colors

Sticky notes on the canvas use soft pastel backgrounds: amber (#FCE68A), neutral (#E4E4E7), rose (#FECDD3), blue (#BFDBFE), lime (#D9F99D).

### Neon / Gradient Accents

Used for flow icon gradients, tool-mode indicators, and decorative color swatches. These are high-saturation, high-energy colors intentionally outside the normal UI palette:

- **Neon Fuchsia** (#FF3276) to **Digital Orchid** (#F480FF): Tool-mode gradient.
- **Plasma Purple** (#7C3AED), **Electric Blue** (#3B10FD): Deep saturated anchors.
- **Holo Frost** (#9AF3FD), **Terminal Green** (#9AFDA9): Light luminous accents.
- **Cosmic Void** (#1A0250): Ultra-dark purple for contrast backgrounds.

## Typography

Two self-hosted font families serve distinct roles in the interface.

- **Pretendard Variable** (sans-serif, variable): The primary UI and display typeface. Used for headings, body text, labels, navigation, buttons, form elements, onboarding, and the Unnest wordmark. It provides consistent Korean and Latin typography across platforms.

- **JetBrains Mono** (monospace): Used exclusively for code -- code blocks, JSON editors, API keys, component IDs, and any machine-readable content. Its programming ligatures and distinct character shapes (especially `0` vs `O`, `1` vs `l`) aid readability in dense technical contexts. Never used for UI labels or headings.

Both families are bundled with the frontend. The product must not depend on external font CDNs.

## Brand

- Display the brand name as **Unnest** only; do not display the Korean transliteration.
- The primary lockup is the supplied symbol on the left with a Pretendard Bold `Unnest` wordmark on the right.
- Use the full lockup on authentication and welcome surfaces. Use the symbol alone in compact navigation, loading, and chat-avatar contexts.
- Render the symbol black in light mode and white in dark mode. The favicon uses a black background with a white symbol.
- Keep surfaces neutral. Nest Green is reserved for primary actions and selected controls; blue remains the informational, selection, and link accent.
- Korean is the default language for new users. Keep technical terms such as Flow, Component, API, MCP, and workflow in English when translation would reduce clarity.

The type scale is compact. The application uses 11px (`xxs`) through 16px (`base`) for the vast majority of UI. Headlines rarely exceed 24px inside the workspace. Density is preferred over visual hierarchy through size alone -- hierarchy is communicated through weight, color, and spatial grouping.

## Layout

The application follows a **sidebar + infinite canvas** model. A collapsible left sidebar (19rem when expanded, 4rem when collapsed to icons) holds navigation, component search, and category panels. The main area is a pannable, zoomable ReactFlow canvas where nodes live.

Spacing follows a strict **4px base grid**:

- **4px** (`xs`): Micro-adjustments -- icon padding within buttons, badge margins, tight inline spacing.
- **8px** (`sm`): Standard internal padding for compact components, gaps between inline elements.
- **16px** (`md`): Card content padding, standard gaps between sibling elements, default component padding.
- **24px** (`lg`): Section margins, dialog padding, sidebar header/footer padding.
- **32px** (`xl`): Major layout divisions, generous whitespace between sections.
- **48px - 64px** (`2xl` - `3xl`): Page-level margins, hero spacing.

**Breakpoints:**
- `mdd: 45rem` (720px) -- medium-density layout shift
- `xl: 1200px` -- wide layout
- `2xl: 1400px` -- container max-width
- `3xl: 1500px` -- extra-wide

The canvas itself is unbounded -- nodes can be placed anywhere. The sidebar is the only fixed-width structural element.

## Elevation & Depth

Elevation is minimal and functional. The design avoids heavy shadows, instead using **border-based containment** and **tonal shifts** to communicate layering. Depth is communicated through three levels:

### Level 0: Canvas

The infinite workspace background. A quiet 1px dotted grid at 24px intervals provides spatial orientation without competing with the flow.

- **Light mode:** Zinc-gray (#F4F4F5) background with 45%-opacity gray dots (#A1A1AA).
- **Dark mode:** Pure black (#000000) background with 35%-opacity dark zinc dots (#3F3F46).

### Level 1: Nodes & Cards

Content containers that sit on the canvas. Neutral cards use a 3px Nest Green rail on the left as the shared Unnest signature.

- **Shadow:** `0 1px 2px rgba(0,0,0,0.04)` at rest and a restrained green-tinted ambient shadow on hover.
- **Selected state:** A Nest Green border with a 15%-opacity 2px ring communicates focus. Error, warning, and frozen borders keep semantic priority.
- **Frozen state:** A special icy glow effect -- `0 0 10px 2px rgba(128,190,230,0.5)` shadow with a 2px `rgba(128,190,219,0.86)` border and frosted overlay. Signals the node is locked from editing.

### Level 2: Popovers & Modals

Overlaid surfaces that demand attention. White background with `shadow-lg` and a border.

- **Entry animation:** Scale from 0.95 with a clip-path reveal, 400ms duration, `cubic-bezier(0.16, 1, 0.3, 1)` easing (spring-like overshoot).
- **Exit animation:** Reverse at 500ms for a slightly more deliberate dismissal.
- **Overlay:** Semi-transparent background to dim the canvas.

### Active States

Interactive feedback is communicated through micro-animations rather than shadow changes:

- **Button press:** `active:scale-[0.97]` -- a subtle inward squeeze.
- **Hover:** Background color shift to accent or muted, never shadow addition.
- **Focus ring:** 1px ring using the `ring` token (Nest Green in light, Unnest Lime in dark).

## Shapes

The shape language is subtly rounded -- not pill-shaped, not sharp. The base radius is **8px** (`--radius: 0.5rem`), which gives cards, buttons, and containers a modern but not toy-like feel.

- **lg (8px):** The default. Cards, modals, buttons, inputs, dropdown content, node containers.
- **md (6px):** Nested or secondary elements -- tooltip content, compact controls, dialog close buttons.
- **sm (4px):** Tight elements -- badges, checkbox corners, compact inline inputs, select items.
- **xl (12px):** Prominent containers -- dialog content, node toolbars, sidebar inset panels.
- **full (9999px):** Circular elements -- avatars, status dots, switch tracks, scrollbar thumbs, badge pills.

Borders are thin. Standard weight is 1px. Occasional 1.5px or 1.75px for emphasis on selected or focus states. All borders use the `border` token color -- never a hard black or white line.

## Components

### Buttons

The button system uses 7 semantic variants:

- **Default (primary):** Nest Green (#3F6212) with white text in light mode; Unnest Lime (#BEF264) with deep green text in dark mode. The highest-emphasis action on any screen.
- **Secondary:** Muted background with border, dark text. For secondary actions alongside a primary button.
- **Destructive:** Red background, white text. Reserved for delete, remove, and irreversible actions.
- **Outline:** Transparent with border. For medium-emphasis actions. Hover fills with the input color.
- **Ghost:** Transparent, no border. For toolbar actions and icon buttons. Hover fills with accent background.
- **Warning:** Dark warning-foreground background. For caution-related actions.
- **Link:** Underlined text with no background. For inline text links.

All buttons share: 40px default height, `rounded-lg` (8px), `font-medium` (500 weight), `text-sm` (14px). Focus state is a 1px ring. Disabled state is `opacity-70` with `pointer-events-none`. The `active:scale-[0.97]` press effect is optional but recommended for primary actions.

**Size variants:** `lg` (44px), `default` (40px), `md` (32px), `sm` (36px), `xs` (compact), and three icon sizes (`iconMd` 6px padding, `icon` 4px padding, `iconSm` 2px padding).

### Badges

Inline status indicators and tags. Always pill-shaped (`rounded-full`) with `font-semibold` text. Available in 6 semantic variants: default (primary fill), gray, secondary (muted fill), destructive, emerald (success), pink, purple (outlined), and error (light red fill). Three size tiers: `sm` (16px height, 12px text), `md` (20px height, 14px text), `lg` (24px height, 16px text), plus `tag` (18px height, 11px text) for compact inline labels.

### Cards

Content containers on surfaces. `rounded-lg` border on muted background with `shadow-sm`. Internal structure: header (16px padding, title in semi-bold 16px, optional description in muted 14px), content area (16px horizontal padding), footer (16px padding, flex row). The title uses `leading-tight tracking-tight` for density.

### Inputs

Text inputs use a bordered white background with rounded-lg corners. Placeholder text is in muted gray (#A1A1AA). Three interaction states: default (border color), hover (border darkens to `muted-foreground`), focus (border goes to `foreground`, placeholder becomes transparent). Disabled inputs get muted background and reduced text. A compact `input-edit-node` variant is used for inline editing within nodes.

### Selects & Dropdowns

Select triggers are 32px height with border and right-aligned chevron. Dropdown content uses the popover surface with `shadow-md`, `rounded-md`, and slide-in animation from the opening side. Items highlight with the accent background on focus. Separators use a 1px muted line.

### Dialogs & Modals

Fixed-position overlays with a white `rounded-xl` container, 24px padding, and `shadow-lg`. Enter with a 400ms spring animation (scale 0.95 to 1.0 with clip-path reveal). Close button in the top-right corner. Title in 18px semi-bold, description in muted 14px. Footer uses `flex-row` with right-aligned actions.

### Tabs

Inline tab bars with no background. The active tab gets a 2px bottom border in `currentColor` (primary). Inactive tabs show text in `muted-foreground`, hovering to `primary`. No rounded backgrounds on tab triggers -- the underline is the sole active indicator.

### Form Controls

**Checkbox:** 16px square, `rounded-sm` (4px), border in muted-foreground. Checked state fills with primary and shows a white check icon. **Switch:** 44x24px track with a 16px thumb. Unchecked track uses the `input` color; checked track uses `primary`. Thumb slides 20px with a transition. **Radio:** 16px circle with primary border, filled center dot when selected.

### Tooltips

Bordered popover surface (not solid black as one might expect) with `rounded-md`, `shadow-md`, 14px text, and directional slide-in animation. Content is `z-[99]` to sit above all other UI.

### Alerts

Rounded bordered containers with icon + text layout. Default variant uses standard foreground colors. Destructive variant tints the border and text with the destructive red, and the icon inherits that color.

### Canvas Nodes

The core UI element of Unnest. Each node is a neutral `rounded-lg` card with a thin Nest Green rail, typed input/output ports, configuration fields, and a status indicator.

- **Ports:** The visible port is an 8×11px vertical egg shape inside an unchanged 32px interaction area. Ports retain the data type color system but use a simple outline on hover instead of neon pulses or glow.
- **Connections:** Neutral 1.5px Bézier lines preserve the existing routing. Selected lines use the source data type color at 2px, falling back to Nest Green; loop outputs remain dashed.
- **Selected nodes:** A Nest Green ring (`node-selected: #3F6212`) replaces the standard border.
- **Frozen nodes** display the icy glow effect with a frosted overlay, signaling they are locked from modification.
- **Canvas controls and toolbars:** Floating `rounded-xl` containers use a translucent neutral surface, subtle border, `shadow-sm`, and Nest Green active states.
- **Status indicators:** Green (built successfully), red (error), yellow (building), gray (idle). A spinner animation shows during active builds.

### Sidebar

Collapsible panel (19rem expanded, 4rem icon-only) with smooth 300ms width transitions. Menu buttons are 32px height, `rounded-md`, with accent background on hover and active states. Supports nested sub-menus with left border indicators. In dark mode, the sidebar has its own dedicated color tokens (darker surface, brighter primary) for visual separation from the canvas.

## Do's and Don'ts

- Do keep structural surfaces neutral and reserve Nest Green for primary actions and selected controls
- Do maintain WCAG AA contrast ratios (4.5:1 for normal text, 3:1 for large text and interactive elements)
- Do use Pretendard Variable for all application UI text; JetBrains Mono only for code and machine-readable content
- Do keep data type colors visually distinct -- they encode connection compatibility and must be recognizable at small sizes (port dots, connection lines)
- Do support both light and dark themes -- every color token must have a dark-mode equivalent
- Do use the 4px base grid for all spacing decisions
- Don't introduce new accent colors without a clear semantic role in the system
- Don't use shadows heavier than the node shadow -- the UI should feel flat, layered through tone rather than depth
- Don't mix border-radius values within the same visual group (all buttons in a toolbar share the same radius)
- Don't load fonts from external CDNs -- both product font families are self-hosted
- Don't use opacity below 0.7 for disabled states -- the element must remain readable
- Don't use solid color fills for hover states on ghost/outline buttons -- use the accent or muted surface tokens
- Don't place data type colors on structural UI elements (borders, backgrounds) outside of the node/port system
