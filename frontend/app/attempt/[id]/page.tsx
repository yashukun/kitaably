import { ExamRunner } from "@/components/exam-runner";

export const metadata = { title: "Sitting" };

/**
 * The runner. Deliberately outside the (app) shell: no navigation, because a page
 * that offers a way to wander off mid-exam is a page somebody will wander off from
 * and lose their answers to.
 */
export default async function AttemptPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <main className="flex flex-1 flex-col">
      <ExamRunner attemptId={id} />
    </main>
  );
}
