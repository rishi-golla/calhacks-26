import CaseTrace from "@/components/cases/case-trace";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <CaseTrace caseId={decodeURIComponent(id)} />;
}
