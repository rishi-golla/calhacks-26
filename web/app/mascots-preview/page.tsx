import { Mascot } from "@/components/mascot";
import { AGENTS } from "@/lib/data";

const all = [{ id: "all", name: "Team chat" }, ...AGENTS];

export default function Page() {
  return (
    <div style={{ fontFamily: "system-ui, sans-serif" }}>
      {[false, true].map((working) => (
        <div key={String(working)} style={{ background: working ? "#15171b" : "#f6f6f4", color: working ? "#e6e8ec" : "#16181d", padding: "20px 28px" }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 12 }}>{working ? "Working" : "Idle"}</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(10, 92px)", gap: 8 }}>
            {all.map((a) => (
              <div key={a.id} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 6 }}>
                <Mascot id={a.id} size={76} working={working} />
                <Mascot id={a.id} size={30} working={working} />
                <small style={{ fontSize: 10.5, opacity: 0.65, textAlign: "center" }}>{a.name}</small>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
