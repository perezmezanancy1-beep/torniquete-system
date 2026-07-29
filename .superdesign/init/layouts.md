# Shared layouts

There is no shared layout component or templating layer. Every route is a
standalone HTML document.

## Administrative shell

- Source: `public/admin.html`
- Description: centered header followed by a 90%-width content container with
  a registration card and a registered-users card.

```html
<body>
  <div class="header">Panel Administrativo</div>
  <div class="container">
    <div class="card"><!-- user form --></div>
    <div class="card"><!-- users table --></div>
  </div>
</body>
```
