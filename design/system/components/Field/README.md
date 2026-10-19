# Field
A label above a control, with optional help text or an error below.

## Use
- Always a visible `<label for>`; inside tables, an `aria-label` per input instead (disc, track, title columns).
- `.input` for text; add `.num` for short numeric inputs (mono, 6rem).
- Help (`.help`) explains effect or format in `small`, linked by `aria-describedby`.
- Invalid: `.field.invalid`, `aria-invalid="true"`, and an `.error` sentence that says what is wrong and what is accepted. The page also lists all errors in a Notice with `role="alert"` above the form.

## The consumer provides
`name`, `required`, `maxlength`, `min`/`max` exactly as the route validates; the server-side error text.
