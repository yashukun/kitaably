import { AttemptResultView } from "@/components/attempt-result";

export const metadata = { title: "Result" };

export default async function ResultPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <main className="flex flex-1 flex-col">
      <AttemptResultView attemptId={id} />
    </main>
  );
}
