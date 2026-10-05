export default function Badge({ value, kind }: { value: string; kind?: string }) {
  return <span className={`badge ${kind ?? value}`}>{value}</span>;
}
