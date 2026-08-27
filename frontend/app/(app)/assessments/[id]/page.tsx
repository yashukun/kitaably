import { AssessmentReview } from "@/components/assessment-review";
import { Shell } from "@/components/glass";

export const metadata = { title: "Paper" };

export default async function AssessmentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Shell title="Paper">
      <AssessmentReview assessmentId={id} />
    </Shell>
  );
}
