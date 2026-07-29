# Extractable components

## AdminHeader

- Source: `public/admin.html`
- Category: layout
- Description: Institutional page title area.
- Extractable props: none
- Hardcoded: title, institutional logo, colors

## AdminCard

- Source: `public/admin.html`
- Category: basic
- Description: White rounded content surface used for forms and tables.
- Extractable props: none
- Hardcoded: radius, padding, surface color

## UserForm

- Source: `public/admin.html`
- Category: basic
- Description: Create/edit form for registered users.
- Extractable props: edit state, selected user
- Hardcoded: field labels and program options

## FingerprintEnrollmentCard

- Source: `public/admin.html`
- Category: basic
- Description: Selects a person and activates the entry or exit reader.
- Extractable props: selected person, selected reader, enrollment state
- Hardcoded: reader labels, guidance steps, status icons

## UsersTable

- Source: `public/admin.html`
- Category: basic
- Description: Searchable table with user actions.
- Extractable props: users, query
- Hardcoded: columns and action icons
