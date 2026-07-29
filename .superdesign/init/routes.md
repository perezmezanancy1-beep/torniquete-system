# Routes

Express serves `public/` as static files and explicitly maps `/` to
`public/index.html`.

| URL | File | Purpose |
| --- | --- | --- |
| `/` | `public/index.html` | Full-screen QR scanning interface |
| `/admin.html` | `public/admin.html` | User administration |
| `/registro.html` | `public/registro.html` | Registration form |
| `/qr.html` | `public/qr.html` | Visitor QR display |
| `/test.html` | `public/test.html` | Diagnostic page |
| fallback | `public/404.html` | Not-found page |

There is no client-side router.
