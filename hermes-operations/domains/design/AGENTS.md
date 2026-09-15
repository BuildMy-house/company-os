# Design Domain — Agent Rules

Extends `shared/AGENTS.base.md`. Read that first.

## Domain: Design

UI/UX design, visual identity, mockups, prototypes, design systems,
accessibility, user research.

## Key Principles

- **User-centered**: Design for the person using the product, not the
  person building it.
- **Accessibility first**: WCAG 2.1 AA minimum for all interfaces.
- **Consistency**: Use the design system. Don't invent new patterns.
- **Evidence-based**: Design decisions backed by research or data.

## Design System

- Component library lives in `site-homely/src/components/`
- Design tokens in `site-homely/src/styles/tokens.css`
- Storybook for component documentation (if applicable)

## Content Types

| Type | Tools | Review |
|------|-------|--------|
| UI mockups | Figma, code | Manager + engineering review |
| Prototypes | Figma, Astro | Manager + user testing |
| Design system | CSS, components | Manager + engineering review |
| Brand assets | Figma, SVG | Manager review |
| Accessibility audit | axe, Lighthouse | Manager + engineering review |

## Workflow

1. Manager receives brief from Hermes
2. Manager creates ticket in PLAN.md with DoD
3. Worker creates designs following design system
4. Manager reviews for quality, accessibility, brand consistency
5. Manager verifies DoD (a11y score, component coverage, etc.)
6. Manager marks done, commits to knowledge base

## Steward Scope

Domain scope path: `house_designer/design`
