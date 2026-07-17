# Accessibility Review

Checklist — report each as pass/fail with the offending element:

1. Keyboard: every action reachable by Tab/Shift-Tab/Enter/Escape; no
   focus traps; focus order follows visual order; focus visible at 3:1
   contrast.
2. Semantics: native elements over ARIA (`button`, `a`, `label`,
   `nav`, `main`); ARIA only when no native equivalent; no redundant
   roles.
3. Contrast: text 4.5:1 (large 3:1), UI components/graphics 3:1, in BOTH
   themes.
4. Forms: every input has a programmatic label; errors are announced and
   described (`aria-describedby`), not color-only.
5. Structure: one `h1`, ordered heading levels, landmarks, page title
   updated on navigation.
6. Media/motion: images have alt text (empty alt for decoration);
   animation respects `prefers-reduced-motion`; nothing flashes >3/s.
7. Dynamic content: live regions for async updates; loading states
   announced; dialogs trap and restore focus.
