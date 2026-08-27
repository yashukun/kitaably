import { LoginForm } from "@/components/login-form";
import { safeNext } from "@/lib/next-path";

export const metadata = { title: "Sign in" };

/**
 * A server component so the return path can be read without `useSearchParams`, which
 * would force a Suspense boundary around a form that has nothing to suspend on.
 */
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  const { next } = await searchParams;
  return <LoginForm next={safeNext(next)} />;
}
