import { ExamEntry } from "@/components/exam-entry";

export const metadata = { title: "Exam" };

/**
 * The share-link entry point. Phase 6.
 *
 * Deliberately outside the (app) shell: it is reached by token and carries no
 * navigation. The token grants access to *attempt*, not an identity — a signed-out
 * visitor is sent to sign in and returned here, because a result has to belong to
 * somebody in order to come back to the paper's author.
 */
export default async function ExamPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return (
    <main className="flex flex-1 items-center justify-center px-5 py-16">
      <ExamEntry token={token} />
    </main>
  );
}
