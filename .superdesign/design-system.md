# UAC Torniquete Admin

## Direction

Functional institutional operations dashboard: clean white surfaces, strong
red hierarchy, compact controls, and unambiguous hardware status. Avoid
decorative gradients behind data-heavy content.

## Tokens

- Brand red: `#C8102E`
- Brand red dark: `#970B22`
- Brand yellow accent: `#F4C430`
- Ink: `#17202A`
- Muted ink: `#667085`
- Canvas: `#F6F7F9`
- Surface: `#FFFFFF`
- Border: `#D8DDE5`
- Success: `#168A45`
- Warning: `#B56A00`
- Error: `#B42318`
- Focus ring: `rgba(200, 16, 46, .22)`
- Font: Arial, Helvetica, sans-serif
- Radius small: `8px`
- Radius card: `14px`
- Shadow: `0 8px 24px rgba(23, 32, 42, .08)`

## Components

- Header: logo at left, title and concise subtitle, red background.
- Cards: white, thin gray border, subtle shadow, clear title and helper text.
- Primary button: solid red; hover dark red; disabled gray.
- Secondary button: white, red border and red text.
- Status badge: icon/dot plus explicit text; color is never the only signal.
- Fingerprint reader selector: two large equal cards labeled Entrada and
  Salida, each with connection/status text.
- Enrollment progress: ordered steps and a live message region.
- Users table: sticky or visually strong header, responsive overflow, textual
  Edit/Delete/Enroll actions.

## Accessibility

- Minimum control height: 44px.
- Visible keyboard focus on every interactive element.
- Labels remain visible above fields.
- Live enrollment updates use `aria-live="polite"`.
- Do not rely on icons or color alone.
