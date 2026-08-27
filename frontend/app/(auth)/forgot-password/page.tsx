import { ForgotPasswordForm } from "@/components/forgot-password-form";

export const metadata = { title: "Reset your password" };

/**
 * A server component so `?error=` — which `/auth/callback` sets when a link is
 * expired or already spent — can be read without `useSearchParams`.
 */
export default async function ForgotPasswordPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;
  return <ForgotPasswordForm initialError={error} />;
}
