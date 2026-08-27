import { redirect } from "next/navigation";

import { SiteNav } from "@/components/site-nav";
import { createClient } from "@/lib/supabase/server";

/**
 * The signed-in shell. One shell, because there is one kind of account.
 *
 * There used to be a `(teacher)` tree and a `(student)` tree that differed in what
 * they rendered. Collapsing them removed the failure mode where a control existed
 * in one tree and its authorization did not exist in the other.
 *
 * This redirect is a convenience, not a security boundary. Anyone can skip it by
 * calling the API directly, which is fine — the backend guard is what refuses a
 * request, and it re-reads the caller from `profiles` every time.
 */
export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) redirect("/login");

  // Name, not role: there is nothing here that branches on what kind of account
  // this is, because there is only one kind.
  const { data: profile } = await supabase
    .from("profiles")
    .select("name, email")
    .eq("id", user.id)
    .single();

  return (
    <div className="flex flex-1 flex-col">
      <SiteNav name={profile?.name ?? profile?.email ?? "Signed in"} />
      {children}
    </div>
  );
}
