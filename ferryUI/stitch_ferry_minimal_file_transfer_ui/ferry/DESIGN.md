---
name: Ferry
colors:
  surface: '#131313'
  surface-dim: '#131313'
  surface-bright: '#393939'
  surface-container-lowest: '#0e0e0e'
  surface-container-low: '#1c1b1b'
  surface-container: '#201f1f'
  surface-container-high: '#2a2a2a'
  surface-container-highest: '#353534'
  on-surface: '#e5e2e1'
  on-surface-variant: '#c4c7c8'
  inverse-surface: '#e5e2e1'
  inverse-on-surface: '#313030'
  outline: '#8e9192'
  outline-variant: '#444748'
  surface-tint: '#c6c6c7'
  primary: '#ffffff'
  on-primary: '#2f3131'
  primary-container: '#e2e2e2'
  on-primary-container: '#636565'
  inverse-primary: '#5d5f5f'
  secondary: '#c7c6c6'
  on-secondary: '#2f3131'
  secondary-container: '#484949'
  on-secondary-container: '#b8b8b8'
  tertiary: '#ffffff'
  on-tertiary: '#2f3131'
  tertiary-container: '#e2e2e2'
  on-tertiary-container: '#636565'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#e2e2e2'
  primary-fixed-dim: '#c6c6c7'
  on-primary-fixed: '#1a1c1c'
  on-primary-fixed-variant: '#454747'
  secondary-fixed: '#e3e2e2'
  secondary-fixed-dim: '#c7c6c6'
  on-secondary-fixed: '#1a1c1c'
  on-secondary-fixed-variant: '#464747'
  tertiary-fixed: '#e2e2e2'
  tertiary-fixed-dim: '#c6c6c7'
  on-tertiary-fixed: '#1a1c1c'
  on-tertiary-fixed-variant: '#454747'
  background: '#131313'
  on-background: '#e5e2e1'
  surface-variant: '#353534'
typography:
  display-lg:
    fontFamily: Inter
    fontSize: 40px
    fontWeight: '600'
    lineHeight: 48px
    letterSpacing: -0.03em
  display-lg-mobile:
    fontFamily: Inter
    fontSize: 32px
    fontWeight: '600'
    lineHeight: 38px
    letterSpacing: -0.025em
  headline-lg:
    fontFamily: Inter
    fontSize: 28px
    fontWeight: '500'
    lineHeight: 36px
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Inter
    fontSize: 22px
    fontWeight: '500'
    lineHeight: 28px
    letterSpacing: -0.015em
  headline-sm:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '500'
    lineHeight: 24px
    letterSpacing: -0.01em
  body-lg:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
    letterSpacing: 0em
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
    letterSpacing: 0em
  body-sm:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
    letterSpacing: 0.01em
  label-code-lg:
    fontFamily: JetBrains Mono
    fontSize: 14px
    fontWeight: '500'
    lineHeight: 20px
    letterSpacing: -0.02em
  label-code-md:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
    letterSpacing: 0em
  label-code-sm:
    fontFamily: JetBrains Mono
    fontSize: 10px
    fontWeight: '400'
    lineHeight: 14px
    letterSpacing: 0.02em
spacing:
  gutter: 1rem
  gutter-lg: 1.5rem
  margin: 1rem
  margin-md: 1.5rem
  margin-lg: 3rem
  space-2xs: 0.125rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 1rem
  space-lg: 1.5rem
  space-xl: 2rem
  space-2xl: 3rem
  space-3xl: 4rem
---

## Brand & Style
The design system embodies modern functionalism and German/Swiss industrial typography. It is engineered for rapid, uncompromising local peer-to-peer data transfers between Android and Linux environments. 

### Brand Character & Emotional Response
- **Sovereign & Private:** No cloud telemetry, no account registration, zero internet reliance. The interface looks and operates like a dedicated system utility, evoking immediate trust, silence, and reliability.
- **Utilitarian Elegance:** Zero decorative flourish, zero skeuomorphism, zero saturated accent hues. Every pixel, hairline border, and text block exists solely to communicate file states, network handshake clarity, and transfer velocity.
- **Modernist Architecture:** Strict grid adherence, structural hairlines, pure monochrome discipline, and systematic rhythm inspired by Braun design standards and DIN norms.

## Colors
The system relies on a stark, calibrated monochrome scale. The primary interface is dark-first to reflect terminal and developer ergonomics, yet translates symmetrically into high-contrast light mode.

### Base Values
- **Canvas Base (Dark):** `#0A0A0A` (Deepest Void)
- **Surface Elevation 1:** `#121212` (Card and peer base)
- **Surface Elevation 2:** `#1A1A1A` (Hover and interactive surfaces)
- **Structural Border (Dark):** `#262626` (Subtle geometric divider)
- **Structural Border (Light):** `#E5E5E5` (Light mode divider)
- **Primary Action / Text:** `#FFFFFF` (High contrast pure white)
- **Secondary / Subdued:** `#A3A3A3` (Balanced mid-gray)
- **Tertiary / Inactive:** `#525252` (Dim metadata, terminal hashes)
- **Canvas Base (Light):** `#FAFAFA`
- **Surface Elevation (Light):** `#FFFFFF`

## Typography
Typographic rhythm balances the mechanical neutrality of Inter with the technical precision of JetBrains Mono for addresses, hashes, port figures, and byte rates.

- **Headlines & Text:** Inter handles all hierarchy, system names, transfer statuses, and descriptive prompts. Headings use medium weights with negative tracking to emulate DIN poster typography.
- **Technical Metrics:** JetBrains Mono is mandatory for file sizes (e.g., `4.28 GB`), transfer speeds (`82.4 MB/s`), SHA-256 validation digests, IP endpoints, and device IDs.
- **Numbers:** All numbers, tabular metrics, and progress percentages render with tabular figures enabled (`tnum`).

## Layout & Spacing
The layout follows a disciplined 8pt grid with 4pt subdivisions for technical metrics and dividers.

### Breakpoints & Adaptability
- **Mobile (Android Handheld, < 600px):** Single-column vertical stream. 16px outer margin. Dynamic drawer and bottom sheets for active pairing codes.
- **Tablet / Split View (600px – 1023px):** 2-column or dual master-detail grid with 24px margins.
- **Desktop (Linux Workstation / Windowed, ≥ 1024px):** Fixed-width application window (680px for standard utility mode, 1080px for extended multi-peer dashboard) centered in canvas with 48px margins.

Gaps and gutters stay tight and structured, prioritizing high information density over decorative emptiness.

## Elevation & Depth
This design system contains **zero drop shadows, blurs, or glow effects**. Elevation is defined strictly through flat tonal planar layering and crisp single-pixel hairlines.

- **Planes:** Base Canvas (`#0A0A0A`) sits beneath Surface 1 (`#121212`). Active, selected, or pressed components lift into Surface 2 (`#1A1A1A`).
- **Dividers:** Strict 1px solid `#262626` borders construct all cards, peer lists, and technical partitions.
- **Focus & Selection:** Visual distinction is rendered using inverted colors (e.g., solid `#FFFFFF` fill with `#000000` text) or a 1px `#FFFFFF` outline, never an ambient shadow.

## Shapes
Geometry is strictly sharp (`0px` corner radii) or subtly softened at micro-scale for touch targets. All cards, dialogs, progress tracks, and peer tiles employ pure right angles. 

Buttons and interactive tags are rectangular slabs. This industrial, boxy geometry reinforces the ethos of a precise Unix-grade system utility.

## Components

### Buttons
- **Primary:** Solid `#FFFFFF` background, `#000000` text, 0px radius, uppercase or sentence case in Inter Medium. No shadow. Hover: `#E5E5E5`. Active: `#CCCCCC`.
- **Secondary / Outline:** Transparent background, 1px solid `#262626` border, `#FFFFFF` text. Hover: `#1A1A1A` background, border turns `#525252`.
- **Destructive:** 1px solid `#FFFFFF` border, inverted red-free pattern (uses strikethrough typography or warning label `[ABORT]` in high contrast white).

### File Transfer Cards & Peer Tiles
- Monolithic rectangular container with a 1px solid `#262626` boundary and `#121212` background.
- Layout splits into three horizontal zones: Peer discovery header (Device Name, OS icon, IP:Port in JetBrains Mono), file queue summary, and real-time numeric readouts.

### Transfer Progress Bar
- Pure flat dual-tone track.
- Container: 2px or 4px solid line with `#262626` background.
- Progress Fill: `#FFFFFF` continuous block without rounded caps or animated shimmer gradients.
- Direct readouts flanking the track: `[==========>---------] 54%` or exact transfer velocity in JetBrains Mono (`42.1 MB/s — ETA 00:14`).

### Chips & Badges
- Minimalist text wrapped in 1px `#262626` borders.
- Background: `#0A0A0A` or `#121212`.
- Text: JetBrains Mono 10px / 12px uppercase (`LOCAL_WIFI`, `HOTSPOT`, `P2P_DIRECT`).

### Checkboxes & Radio Selectors
- Checkboxes are 16x16px sharp squares with a 1px `#525252` border. Checked state: Solid `#FFFFFF` fill with an inverted black checkmark or inner solid black square.
- Radio buttons: 16x16px concentric sharp squares. Selected state: 8x8px solid `#FFFFFF` inner square centered inside `#000000` fill.

### Input Fields & Terminal Consoles
- Flat `#0A0A0A` field, bottom or full-perimeter 1px border `#262626`.
- Focus State: Border transitions to 1px `#FFFFFF` instantly with no glow.
- Monospace cursor: Blinking block or hairline vertical beam.