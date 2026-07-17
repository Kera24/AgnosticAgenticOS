# Frontend Design

Guidance for building interfaces that look intentional, not templated.

- Commit to one visual direction and execute it consistently; do not mix
  design languages within a view.
- Typography first: pick at most two typefaces, define a scale (e.g.
  1.25 ratio), and use weight/size — not color — for hierarchy.
- Restrained palette: one accent color, neutrals for everything else;
  verify WCAG AA contrast (4.5:1 body text, 3:1 large text).
- Spacing system: a single base unit (4 or 8 px) applied everywhere;
  irregular spacing reads as broken, not creative.
- Avoid: gradient-on-everything, glassmorphism stacks, three-shadow cards,
  emoji as icons, animation without purpose.
- Motion: 150–250 ms transitions for state changes only; honour
  `prefers-reduced-motion`.
- Every interactive element needs visible focus, hover, active, disabled
  states, and a loading/empty/error treatment.
- Build responsively from a 360 px viewport up; never rely on horizontal
  page scrolling.
