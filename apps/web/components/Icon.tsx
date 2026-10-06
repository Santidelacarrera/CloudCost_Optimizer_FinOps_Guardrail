// Iconos de trazo (20 px). Decorativos: el texto de al lado siempre dice lo mismo.
const P: Record<string, string> = {
  panel: "M3.5 10.5 10 4l6.5 6.5M5.5 9v7h9V9",
  list: "M7 5.5h9M7 10h9M7 14.5h9M3.6 5.5h.01M3.6 10h.01M3.6 14.5h.01",
  upload: "M10 13.5V4.5M6.5 8 10 4.5 13.5 8M4 14v1.5h12V14",
  receipt: "M5 3.5h10v13l-2-1.3-1.5 1.3L10 15.2 8.5 16.5 7 15.2 5 16.5v-13ZM7.5 7.5h5M7.5 10.5h5",
  shield: "M10 3.2 15.8 5.5v4.2c0 3.2-2.3 5.7-5.8 7.1-3.5-1.4-5.8-3.9-5.8-7.1V5.5L10 3.2ZM7.6 10l1.8 1.8 3.2-3.4",
  users: "M7.5 9a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5ZM2.8 16c.3-2.4 2.3-4 4.7-4s4.4 1.6 4.7 4M13 8.4a2.1 2.1 0 1 0-.6-4.1M14 11.6c1.6.3 2.9 1.6 3.2 3.4",
  user: "M10 9.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM4.2 16.5c.4-2.6 2.7-4.5 5.8-4.5s5.4 1.9 5.8 4.5",
  out: "M8 4H4.5v12H8M12 6.5 15.5 10 12 13.5M15.5 10H7.5",
};
export default function Icon({ name, size = 20 }: { name: keyof typeof P | string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d={P[name] ?? ""} />
    </svg>
  );
}
