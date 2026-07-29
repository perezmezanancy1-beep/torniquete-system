# Page dependency trees

## `/admin.html` — Administrative panel

Entry: `public/admin.html`

Dependencies:

- `public/admin.html`
  - Firebase Web SDK 8.10.0 (CDN)
  - jQuery 3.6.0 (CDN)
  - Select2 4.1.0 (CDN)
  - Firebase Realtime Database `usuarios`
  - Backend endpoints under `/api/huellas`

The page is monolithic: markup, styles, and behavior are all inline.

## `/` — QR scanner

Entry: `public/index.html`

Dependencies:

- `public/index.html`
  - `public/img/logo1.png`

## `/qr.html` — QR display

Entry: `public/qr.html`

Dependencies:

- `public/qr.html`
  - backend `POST /api/qr`
