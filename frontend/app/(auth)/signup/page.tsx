import { SignupForm } from "@/components/signup-form";
import { safeNext } from "@/lib/next-path";

export const metadata = { title: "Create an account" };

export default async function SignupPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  const { next } = await searchParams;
  return <SignupForm next={safeNext(next)} />;
}
