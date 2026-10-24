import NodeView from "@/components/node-view";
export default async function NodePage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ case?: string }>;
}) {
  const { id } = await params;
  const { case: caseId = "" } = await searchParams;
  return <NodeView id={id} caseId={caseId} />;
}
