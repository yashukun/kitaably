import { AssessmentList } from "@/components/assessment-list";
import { Shell } from "@/components/glass";

export const metadata = { title: "Assessments" };

export default function AssessmentsPage() {
  return (
    <Shell title="Assessments">
      <p className="mb-8 max-w-2xl text-sm leading-relaxed text-parchment-dim">
        Write a paper from any of your books — or anything shared — publish it, and
        send the link to whoever should sit it. Their results come back to you.
      </p>
      <AssessmentList />
    </Shell>
  );
}
