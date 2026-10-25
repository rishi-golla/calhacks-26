import { redirect } from "next/navigation";

/** The legacy admin console was retired; its decision log lives in the Log overlay on every page. */
export default function Ops() {
  redirect("/");
}
