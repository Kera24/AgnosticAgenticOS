# UI/UX Design Systems

- Define tokens before components: color roles (background, surface, text,
  accent, danger…), spacing, radii, type scale — as CSS custom properties
  or the project's token format. Components consume tokens, never raw
  values.
- Support light and dark themes by swapping token values, not component
  styles; test both.
- One component, one responsibility: variants via explicit props
  (size/tone/state), not ad-hoc class overrides.
- States are part of the component contract: default, hover, focus-visible,
  active, disabled, loading, error, empty.
- Document each component where it lives (props table + usage example);
  undocumented components rot.
- Prefer composition over configuration; a component with 12 boolean props
  is three components.
- Never introduce a second slightly-different button/input/dialog; extend
  the existing one.
