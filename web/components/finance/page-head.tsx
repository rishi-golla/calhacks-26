export function PageHead({ title, sub }: { title: string; sub: string }) {
  return (
    <header className="fm-page-head fl-reveal">
      <h1 className="fl-display">{title}</h1>
      <p>{sub}</p>
    </header>
  );
}
