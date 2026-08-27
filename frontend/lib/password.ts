/**
 * The minimum password length, in one place.
 *
 * It has to agree with `[auth] minimum_password_length` in `supabase/config.toml`,
 * because **the form input is not the check**. `minLength` is an affordance a
 * signed-out caller can skip entirely by posting to GoTrue directly; the config is
 * what actually refuses a short password. When these two disagree the UI is simply
 * lying about the rule, in whichever direction.
 */
export const MIN_PASSWORD_LENGTH = 12;
